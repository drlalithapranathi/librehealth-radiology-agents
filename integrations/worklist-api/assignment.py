"""Radiologist assignment reader for the Worklist API (M2 issue #20).

Assignment is OWNED BY LH-Radiology (specialty + case importance + call times)
— this is a CLAUDE.md locked decision. The Worklist API reads it and never
writes it.

For M2, LH-Radiology's assignment source is not yet wired in the dev stack —
`NullAssignmentReader` returns None for every study, matching how a fresh
worklist shows unassigned studies. When LH-Radiology's real assignment source
is available (M3 or when the RIS integration lands), a real reader implements
the same `AssignmentReader` protocol and drops in at the FastAPI factory —
callers on this side are contract-stable.

Owner: Parvati.
"""
from __future__ import annotations

from typing import Optional, Protocol


class AssignmentReader(Protocol):
    """Read-only radiologist assignment for a study.

    Returns `{"radiologistId": str, "assignedAt": iso8601}` or None if the study
    isn't assigned. Never raises; a source outage returns None (unassigned) so the
    worklist still serves — a missing assignment is a valid, not-error state."""

    async def get(self, study_instance_uid: str) -> Optional[dict]: ...


class NullAssignmentReader:
    """No-op reader used in dev / until LH-Radiology assignment is wired.

    Every study reads as unassigned; the OHIF client should render the "Assign to
    me" affordance for all rows in this mode."""

    async def get(self, study_instance_uid: str) -> Optional[dict]:
        return None
