"""OpenTelemetry bootstrap gate (#28). The [otel] extra is optional, so skip if it isn't installed.

These tests exercise the env gate, the installed-check, and the no-op paths on purpose: none of them
installs a global TracerProvider, so they stay order-independent and never leak an exporter into the
rest of the suite. Real span export -- the worker's interceptor wiring and the ingress's server
spans, both against an in-memory exporter -- is covered in orchestrator/tests/test_worker_tracing.py.
"""
from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry", reason="radagent-common[otel] not installed")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from radagent_common import tracing as tracing_mod  # noqa: E402
from radagent_common.tracing import tracing_enabled, init_tracing  # noqa: E402

_OTEL_ENV = ("OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_TRACES_EXPORTER")


def _clear(monkeypatch) -> None:
    for k in _OTEL_ENV:
        monkeypatch.delenv(k, raising=False)


def test_disabled_by_default(monkeypatch):
    """No OTEL_* env -> tracing off and init_tracing installs no SDK provider (tests stay quiet)."""
    _clear(monkeypatch)
    assert tracing_enabled() is False
    init_tracing("noop")
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)


def test_sdk_disabled_flag_forces_off(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")   # would enable...
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")         # ...but this wins
    assert tracing_enabled() is False
    init_tracing("noop")
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)


def test_enabled_by_env(monkeypatch):
    """Either the OTLP endpoint or a non-none OTEL_TRACES_EXPORTER flips the gate on."""
    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    assert tracing_enabled() is True
    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert tracing_enabled() is True
    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")      # explicit none -> still off
    assert tracing_enabled() is False


def test_new_ids_follow_the_active_trace(monkeypatch):
    """Enabled + a span active -> new_trace_id/new_span_id ARE the current trace/span ids, so
    meta.traceId correlates the envelope with the study's distributed trace (#28). Uses a LOCAL
    provider (never set as global) so it leaks no exporter into the rest of the suite."""
    from radagent_common.tracing import new_span_id, new_trace_id

    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    assert tracing_enabled() is True
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("study") as span:
        sctx = span.get_span_context()
        assert new_trace_id() == trace.format_trace_id(sctx.trace_id)
        assert new_span_id() == trace.format_span_id(sctx.span_id)


def test_configured_but_extra_missing_degrades_to_off(monkeypatch, caplog):
    """Tracing is observability, never a dependency of the pipeline. If an image is built without
    the [otel] extra but the env asks for tracing, the gate must return False -- every call site
    imports OpenTelemetry lazily behind it, so a True here would raise ModuleNotFoundError at
    startup and crash-loop the service. Degrade to off, and say so once."""
    _clear(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(tracing_mod, "_otel_installed", lambda: False)
    monkeypatch.setattr(tracing_mod, "_warned_missing", False)

    assert tracing_enabled() is False
    assert "not installed" in caplog.text.lower()
    init_tracing("noop")                                    # still a no-op, still no provider
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
