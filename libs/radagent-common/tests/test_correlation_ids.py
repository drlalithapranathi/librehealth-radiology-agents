"""W3C trace-context id format for the StudyContext envelope (#28).

No [otel] extra needed: with tracing disabled, new_trace_id/new_span_id mint fresh random W3C ids,
and they must match contracts/studycontext.schema.json's meta.traceId / meta.spanId patterns.
"""
from __future__ import annotations
import json
import re

from radagent_common import paths
from radagent_common.tracing import new_span_id, new_trace_id

_OTEL_ENV = ("OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_TRACES_EXPORTER")


def _tracing_off(monkeypatch) -> None:
    for k in _OTEL_ENV:
        monkeypatch.delenv(k, raising=False)


def test_generated_ids_are_w3c_hex(monkeypatch):
    _tracing_off(monkeypatch)
    for _ in range(20):
        assert re.fullmatch(r"[0-9a-f]{32}", new_trace_id())
        assert re.fullmatch(r"[0-9a-f]{16}", new_span_id())


def test_generated_ids_satisfy_the_studycontext_schema(monkeypatch):
    """The generators track the contract: read the real patterns and match against them, so a future
    schema/generator drift fails here."""
    _tracing_off(monkeypatch)
    schema = json.loads((paths.contracts_dir() / "studycontext.schema.json").read_text())
    props = schema["properties"]["meta"]["properties"]
    assert re.match(props["traceId"]["pattern"], new_trace_id())
    assert re.match(props["spanId"]["pattern"], new_span_id())
