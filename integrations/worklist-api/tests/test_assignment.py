"""Tests for the AssignmentReader interface + NullAssignmentReader default."""
from __future__ import annotations

import pytest

from assignment import AssignmentReader, NullAssignmentReader


async def test_null_reader_returns_none_for_any_study():
    """The dev-stack default: no LH-Radiology yet, so every study is unassigned.
    OHIF should render the 'Assign to me' affordance for all rows."""
    reader = NullAssignmentReader()
    assert await reader.get("1.2.3") is None
    assert await reader.get("any-uid-at-all") is None
    assert await reader.get("") is None


def test_null_reader_conforms_to_protocol():
    """The AssignmentReader Protocol lets us drop in a real LhRadiologyAssignmentReader
    later without touching callers. Sanity check: NullAssignmentReader satisfies it."""
    reader: AssignmentReader = NullAssignmentReader()  # type-check + instantiation
    assert hasattr(reader, "get")
