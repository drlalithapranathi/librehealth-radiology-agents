"""Correlation helpers + OpenTelemetry bootstrap (#28).

The `new_trace_id`/`new_span_id`/`now_iso` helpers stay as-is (the opaque `trc_`/`spn_` ids the
StudyContext envelope carries today); redefining `meta.traceId` as W3C is a separate, contract-gated
slice. `init_tracing` is the OTel seam: it is OPT-IN and imports OpenTelemetry LAZILY, so importing
this module never pulls in the SDK and code paths that don't call it (all tests) keep the API's
no-op tracer at zero cost."""
from __future__ import annotations
import importlib.util
import logging
import os
import uuid
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_warned_missing = False

# EVERY module the gated call sites lazily import must be listed here, or the gate answers
# "installed" and the import explodes anyway -- the exact crash-loop this check exists to prevent.
# Keep in sync with the imports guarded by tracing_enabled():
#   tracing.init_tracing  -> opentelemetry.sdk, opentelemetry.exporter.otlp.proto.http
#   orchestrator.worker   -> opentelemetry.instrumentation.httpx (+ temporalio.contrib.opentelemetry,
#                            vendored with temporalio; it needs only the API/SDK below)
#   orchestrator.ingress  -> opentelemetry.instrumentation.fastapi, .httpx
#   radagent_common.a2a   -> opentelemetry.instrumentation.starlette, .httpx
# Probed with find_spec, which does NOT execute the module, so the gate itself stays import-free.
_OTEL_MODULES = (
    "opentelemetry.sdk",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.starlette",
    "opentelemetry.instrumentation.httpx",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    return "trc_" + uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return "spn_" + uuid.uuid4().hex[:12]


def _otel_configured() -> bool:
    """The env gate alone: is OTel export asked for?"""
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return True
    return os.environ.get("OTEL_TRACES_EXPORTER", "").lower() not in ("", "none")


def _otel_installed() -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in _OTEL_MODULES)
    except (ImportError, ValueError):  # a partially-installed namespace package
        return False


def tracing_enabled() -> bool:
    """True iff OpenTelemetry export is BOTH configured (env) AND installed (the [otel] extra).

    Callers gate every OTel import, interceptor, and instrumentation on this, so it keeps the extra
    genuinely optional at runtime and tests completely otel-free. Configured by
    `OTEL_EXPORTER_OTLP_ENDPOINT` (the compose collector) or `OTEL_TRACES_EXPORTER` in
    {console,otlp}; `OTEL_SDK_DISABLED=true` forces off.

    The installed-check is what keeps a misconfiguration from being fatal: the gated imports are
    lazy, so if the env asks for tracing in an image built WITHOUT the extra, an unguarded gate
    would raise ModuleNotFoundError at startup and crash-loop the service. Tracing is observability,
    never a dependency of the pipeline -- so we degrade to off and say so, loudly, once."""
    if not _otel_configured():
        return False
    if not _otel_installed():
        global _warned_missing
        if not _warned_missing:
            _warned_missing = True
            _log.warning(
                "OpenTelemetry export is configured but the [otel] extra is not installed; "
                "running WITHOUT tracing. Install radagent-common[otel] to enable it."
            )
        return False
    return True


def init_tracing(service_name: str) -> None:
    """Install the OpenTelemetry SDK tracer provider for a service entrypoint (#28). Idempotent, and
    a NO-OP unless tracing_enabled() -- so tests + un-configured runs create nothing. Chooses the
    OTLP exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set (the compose collector), else a console
    exporter for local dev. Requires the `radagent-common[otel]` extra; imported lazily."""
    if not tracing_enabled():
        return
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    # Idempotent: once an SDK provider is installed, a second call is a no-op (OTel warns on reset).
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter: object = OTLPSpanExporter()
    else:
        exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))  # type: ignore[arg-type]
    trace.set_tracer_provider(provider)
