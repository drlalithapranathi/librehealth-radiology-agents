"""Orthanc Python plugin — fires the orchestrator webhook when a study becomes stable.

Wiring notes
------------
The Orthanc Python plugin does NOT expose a discrete ``RegisterOnStableStudyCallback``.
Lifecycle events go through ``RegisterOnChangeCallback(fn)`` where
``fn(changeType, level, resourceId)``, and STABLE_STUDY is one of the change-type
constants (see the Orthanc SDK's ``OrthancPluginChangeType`` enum). We filter on
that constant and fetch the study's tags via
``RestApiGet('/studies/{id}?requested-tags=ModalitiesInStudy')`` — the same source
of truth the Lua fallback uses, so both paths emit a byte-identical payload and the
orchestrator ingress cannot tell which mechanism fired. ``occurredAt`` is reshaped
from Orthanc's ``LastUpdate`` (DICOM ``YYYYMMDDTHHMMSS``) into RFC 3339.

Deploy
------
Load via Orthanc's Python plugin (``PythonScript`` in ``orthanc.json``). If Python
plugin packaging is awkward for a given deployment, the Lua fallback
(``orthanc_stable_study.lua``) POSTs the identical JSON body — load one or the
other, never both.

Owner: Parvati. Trigger map: ARCHITECTURE.md
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# The `orthanc` module is provided by the Orthanc Python plugin runtime. When this
# file is imported OUTSIDE Orthanc (e.g. from unit tests), the import fails and the
# pure helpers below stay usable. Callback registration only happens when the
# module is present, so importing the file at test time has no side effect.
try:
    import orthanc  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only reachable in the Orthanc runtime
    orthanc = None  # type: ignore[assignment]


ORCH_WEBHOOK = os.environ.get("ORCH_WEBHOOK_URL", "http://orchestrator:8090/webhooks/orthanc")


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable outside Orthanc.
# ---------------------------------------------------------------------------


def now_iso_utc() -> str:
    """RFC 3339 UTC "now" — matches the Lua fallback's ``nowIsoUtc()``. Used as
    the fall-through for ``occurredAt`` so the event stays schema-valid when a
    build's study record omits ``LastUpdate``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_rfc3339_utc(orthanc_dt) -> str | None:
    """Convert an Orthanc ``LastUpdate`` timestamp to RFC 3339, or None if it is
    empty/unparseable. Orthanc reports ``LastUpdate`` in the DICOM datetime shape
    ``YYYYMMDDTHHMMSS`` (UTC), which is NOT what the schema's ``format: date-time``
    (RFC 3339) requires, so we reshape it to ``YYYY-MM-DDTHH:MM:SSZ``. A value that
    is already RFC 3339 (some builds) is passed through unchanged. Mirrors the Lua
    fallback's ``toRfc3339Utc``."""
    if not isinstance(orthanc_dt, str) or not orthanc_dt:
        return None
    try:
        return datetime.strptime(orthanc_dt, "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        try:  # already RFC 3339? keep it as-is
            datetime.fromisoformat(orthanc_dt.replace("Z", "+00:00"))
            return orthanc_dt
        except ValueError:
            return None


def is_http_url(url: str) -> bool:
    """``urllib.request.urlopen`` also dereferences ``file:``/``ftp:`` schemes
    (Bandit B310 / CWE-939). ``ORCH_WEBHOOK`` is operator-configured, but
    constraining the scheme stops an accidental or tampered non-HTTP URL from
    ever being fetched. Mirrors the Lua fallback's ``isHttpUrl`` guard."""
    return urllib.parse.urlparse(url).scheme in ("http", "https")


def build_event(orthanc_study_id: str, study_record: dict | None) -> dict:
    """Assemble a schema-valid ``OrthancStableStudyEvent`` from a
    ``/studies/{id}`` REST view of the study. Field-for-field identical to the
    Lua fallback's ``buildEvent`` — the ingress cannot tell which path fired.

    Sources tags via the REST view (rather than the callback's ``resource_id``
    alone) because that is the single portable shape across every Orthanc build.
    """
    main_tags = (study_record or {}).get("MainDicomTags") or {}
    # ModalitiesInStudy is a computed study tag Orthanc only returns when explicitly
    # requested (?requested-tags=), so it lands in RequestedTags, not MainDicomTags.
    # Some builds also park AccessionNumber there.
    requested_tags = (study_record or {}).get("RequestedTags") or {}
    accession = main_tags.get("AccessionNumber") or requested_tags.get("AccessionNumber") or ""
    modality = (requested_tags.get("ModalitiesInStudy")
                or main_tags.get("ModalitiesInStudy")
                or main_tags.get("Modality") or "")
    study_uid = main_tags.get("StudyInstanceUID") or ""
    occurred_at = to_rfc3339_utc((study_record or {}).get("LastUpdate")) or now_iso_utc()
    return {
        "schemaVersion": "1.0.0",
        "eventType": "orthanc.study.stable",
        "orthancStudyId": orthanc_study_id,
        "studyInstanceUID": study_uid,
        "modality": modality,
        "accessionNumber": accession,
        "occurredAt": occurred_at,
    }


def _post(payload: dict) -> None:
    if not is_http_url(ORCH_WEBHOOK):
        raise ValueError(f"refusing non-HTTP(S) orchestrator webhook URL: {ORCH_WEBHOOK!r}")
    req = urllib.request.Request(
        ORCH_WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # scheme constrained to http(s) above, so urlopen cannot reach file:/ftp:
    urllib.request.urlopen(req, timeout=10)  # nosemgrep


# ---------------------------------------------------------------------------
# Orthanc callback glue — only wired when the runtime module is available.
# ---------------------------------------------------------------------------


def on_change(change_type, level, resource_id) -> None:
    """Filter for STABLE_STUDY, fetch the study record via ``RestApiGet``, build
    the event payload, POST. Any error is swallowed and logged — a downstream
    orchestrator outage must never take down the PACS."""
    if orthanc is None:  # pragma: no cover - only reachable via test-time misuse
        return
    if change_type != orthanc.ChangeType.STABLE_STUDY:
        return
    try:
        # ?requested-tags=ModalitiesInStudy makes Orthanc compute+return the study
        # modality (absent from MainDicomTags by default). Same query as the Lua path.
        study_record = json.loads(
            orthanc.RestApiGet(f"/studies/{resource_id}?requested-tags=ModalitiesInStudy")
        )
    except Exception as e:  # noqa: BLE001 - unreadable study — log and drop
        orthanc.LogError(f"OnChange(STABLE_STUDY): failed to read study {resource_id}: {e}")
        return
    payload = build_event(resource_id, study_record)
    try:
        _post(payload)
    except Exception as e:  # noqa: BLE001 - never crash the PACS on a webhook failure
        orthanc.LogError(f"orchestrator webhook failed: {e}")


if orthanc is not None:  # pragma: no cover - only in the Orthanc runtime
    orthanc.RegisterOnChangeCallback(on_change)
