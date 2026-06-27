"""Minimal correlation helpers. Swap for OpenTelemetry in M3 (see architecture notes: cross-agent tracing)."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    return "trc_" + uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return "spn_" + uuid.uuid4().hex[:12]
