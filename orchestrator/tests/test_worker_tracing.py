"""Tracing wiring on the worker + ingress (#28).

These pin the three ways the instrumentation can be present but useless -- each one fails
SILENTLY (no error, just wrong or missing spans), so only a test catches a regression:

1. the TracingInterceptor handed to BOTH the client and the Worker -> every span emitted twice;
2. FastAPI instrumented inside the lifespan, after Starlette froze its middleware stack -> the
   ingress exports nothing at all;
3. tracing configured in an image built without the [otel] extra -> ModuleNotFoundError at
   startup, crash-looping a service over an observability feature.

Skipped unless temporalio is installed; the otel-specific cases skip without the extra.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio.testing import WorkflowEnvironment  # noqa: E402

from orchestrator.worker import build_worker, tracing_interceptors  # noqa: E402
from radagent_common import paths, tracing  # noqa: E402

otel = pytest.importorskip("opentelemetry.sdk", reason="[otel] extra not installed")


# --- 1. the worker must NOT re-add the client's interceptor ------------------------

def test_build_worker_passes_no_interceptors():
    """Temporal prepends the client's interceptors to the worker's own list without de-duping,
    so passing TracingInterceptor to both double-spans every workflow and activity."""
    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            worker = build_worker(env.client)
            assert worker.config()["interceptors"] == []
    asyncio.run(scenario())


# --- 2. the client gets exactly one interceptor, and only when enabled ---------------

def test_tracing_interceptors_empty_when_tracing_is_off(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    assert tracing_interceptors() == []


def test_tracing_interceptors_is_exactly_one_when_enabled(monkeypatch):
    """Exactly one -- the client's. Both of the real side effects here are PROCESS-GLOBAL (the SDK
    tracer provider and the httpx instrumentation), so they are stubbed/undone: a test that leaves
    a global exporter installed silently changes what every later test sees."""
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    import orchestrator.worker as worker_mod
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from temporalio.contrib.opentelemetry import TracingInterceptor

    monkeypatch.setattr(worker_mod, "init_tracing", lambda _name: None)  # no global provider
    try:
        interceptors = worker_mod.tracing_interceptors()
        assert len(interceptors) == 1
        assert isinstance(interceptors[0], TracingInterceptor)
    finally:
        HTTPXClientInstrumentor().uninstrument()  # process-global: never leak it to other tests


# --- 3. configured-but-not-installed degrades to OFF, it does not crash --------------

def test_tracing_disabled_when_configured_but_extra_is_missing(monkeypatch, caplog):
    """An image built without [otel] + OTEL_* set in the env must run WITHOUT tracing, not
    crash-loop: every gated call site lazily imports OpenTelemetry behind this one gate."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(tracing, "_otel_installed", lambda: False)
    monkeypatch.setattr(tracing, "_warned_missing", False)

    assert tracing.tracing_enabled() is False          # degraded, not fatal
    assert tracing_interceptors() == []                # so nothing imports opentelemetry
    assert "not installed" in caplog.text.lower()      # and it says so, loudly


# --- 4. the ingress instruments ITSELF at import, not inside the lifespan -------------

# Runs in a SUBPROCESS: importing ingress with tracing on installs a global TracerProvider and
# patches httpx process-wide, which must not leak into this suite. It also has to be a fresh
# interpreter -- the placement bug is an IMPORT-TIME property, and orchestrator.ingress is already
# in sys.modules here (imported, untraced, at collection).
#
# The assertion is the one that actually discriminates: does `app` come back from `import` ALREADY
# instrumented? Instrumenting inside the lifespan cannot satisfy that -- Starlette has cached its
# middleware stack by then, so the call is a silent no-op and the flag is never set. An earlier
# version of this test instrumented the app itself and then checked for spans, which passed against
# the buggy ingress too; it guarded nothing.
_IMPORT_PROBE = """
import asyncio, httpx
from orchestrator.ingress import app   # <- the import under test; it must self-instrument

async def hit():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/healthz")

status = asyncio.run(hit()).status_code     # a real request through the real ASGI stack
print("INSTRUMENTED", bool(getattr(app, "_is_instrumented_by_opentelemetry", False)))
print("STATUS", status)
# Spans (if any) went to the console exporter on stdout; flush them before we exit.
from opentelemetry import trace
provider = trace.get_tracer_provider()
if hasattr(provider, "force_flush"):
    provider.force_flush()
"""


def _import_ingress_in_subprocess(env_extra: dict) -> str:
    import os
    import subprocess

    env = {**os.environ, "PYTHONPATH": str(paths.repo_root()), **env_extra}
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    env.pop("OTEL_TRACES_EXPORTER", None)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], capture_output=True, text=True, env=env, timeout=120
    )
    assert out.returncode == 0, f"importing orchestrator.ingress failed:\n{out.stderr[-2000:]}"
    return out.stdout


def test_ingress_instruments_itself_at_import_when_tracing_is_on():
    out = _import_ingress_in_subprocess({"OTEL_TRACES_EXPORTER": "console"})
    assert "INSTRUMENTED True" in out, out          # the app came back from import already traced
    assert "STATUS 200" in out, out
    assert '"name": "GET /healthz"' in out, out     # ...and a real request really exported a span


def test_ingress_is_untouched_when_tracing_is_off():
    """The default posture: no OTEL_* env -> no provider, no instrumentation, no spans at all."""
    out = _import_ingress_in_subprocess({})
    assert "INSTRUMENTED False" in out, out
    assert "STATUS 200" in out, out                 # the ingress still serves, just untraced
    assert '"name": "GET /healthz"' not in out, out
