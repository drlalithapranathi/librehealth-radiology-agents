"""The worker registers EVERY activity the workflow can call (#26).

This is the systemic guard for the bug class that shipped on !38: worker.py imported an activity
that did not exist, `python -m orchestrator.worker` could not start -- and the whole suite passed
green, because nothing imports orchestrator.worker and the e2e test hand-builds its own Worker with
its own activity list. worker.py is the ONLY place the real activity list is assembled, and it had
no guard at all.

Two assertions, and the second is the one that matters:
  1. orchestrator.worker imports (catches the literal !38 ImportError).
  2. The list it registers is COMPLETE -- every @activity.defn in activities.py is on it, and every
     ACT_* name the workflow dispatches by string resolves to one of them. An import smoke test
     alone would not catch a NEW activity that someone forgets to register: the module imports
     fine, and the workflow fails at runtime when it dispatches a name the worker never registered.
"""
from __future__ import annotations

import pytest

pytest.importorskip("temporalio", reason="orchestrator deps not installed")

import orchestrator.activities as activities  # noqa: E402
import orchestrator.state as state  # noqa: E402
import orchestrator.worker as worker  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402


def _defined_activities() -> dict[str, object]:
    """Every @activity.defn in activities.py, keyed by its REGISTERED name (the Temporal name,
    which is what the workflow dispatches by -- not the python function name)."""
    found = {}
    for name in dir(activities):
        fn = getattr(activities, name)
        defn = getattr(fn, "__temporal_activity_definition", None)
        if defn is not None:
            found[defn.name] = fn
    return found


def _registered_activities() -> dict[str, object]:
    """Exactly what worker.py hands to the Worker -- read off worker.ACTIVITIES itself, never a
    copy declared here. A guard that re-declares the list it is guarding drifts, and a drifting
    guard is not a guard."""
    by_fn = {fn.__name__: (defn_name, fn)
             for defn_name, fn in _defined_activities().items()}
    registered = {}
    for fn in worker.ACTIVITIES:
        defn_name, real = by_fn[fn.__name__]
        registered[defn_name] = real
    return registered


def test_the_worker_module_imports():
    """!38's literal failure: worker.py imported write_presign_impression_activity before it
    existed, so `python -m orchestrator.worker` died on startup while CI stayed green."""
    assert worker.TASK_QUEUE == state.TASK_QUEUE
    assert worker.ACTIVITIES


def test_every_activity_defined_is_registered_on_the_worker():
    """The real guard. A new @activity.defn that nobody adds to worker.py imports fine and fails
    at RUNTIME, when the workflow dispatches a name the worker never registered."""
    defined = set(_defined_activities())
    registered = set(_registered_activities())
    missing = defined - registered
    assert not missing, (
        f"activities defined in activities.py but NOT registered on the worker: {sorted(missing)}. "
        "Add them to the activities=[...] list in orchestrator/worker.py."
    )


def test_every_activity_name_the_workflow_dispatches_is_registered():
    """The workflow calls activities by STRING (the ACT_* constants), so a typo or an unregistered
    name is invisible until a study reaches that step. Every name the workflow can dispatch must
    resolve to an activity the worker actually serves."""
    dispatched = {getattr(state, n) for n in dir(state) if n.startswith("ACT_")}
    registered = set(_registered_activities())
    missing = dispatched - registered
    assert not missing, (
        f"the workflow dispatches activity names the worker does not register: {sorted(missing)}"
    )
    # ...and the workflow is the only thing that should be dispatching them.
    assert StudyWorkflow is not None
