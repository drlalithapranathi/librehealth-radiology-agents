"""Temporal worker: hosts StudyWorkflow + activities on the study task queue."""
from __future__ import annotations
import asyncio
import logging
import os
from temporalio.client import Client
from temporalio.worker import Worker

from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .activities import (
    call_agent_skill_activity,
    start_agent_skill_activity,
    publish_priority_activity,
    write_presign_impression_activity,
    escalate_activity,
    load_escalation_policy_activity,
    record_policy_failure_activity,
)

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
_log = logging.getLogger(__name__)


async def _connect_with_retry(target: str, attempts: int = 60, delay: float = 2.0) -> Client:
    """Temporal (auto-setup) is usually not ready when the container starts, so retry the
    connect instead of crashing the worker on the first failed attempt."""
    for i in range(1, attempts + 1):
        try:
            return await Client.connect(target)
        except Exception as exc:  # noqa: BLE001 - broad on purpose while Temporal is booting
            if i == attempts:
                raise
            _log.warning("Temporal not ready at %s (attempt %d/%d): %s", target, i, attempts, exc)
            await asyncio.sleep(delay)


# The activity list the study worker serves. A module-level constant, not a literal buried in
# main(), so test_worker_registration.py can assert it is COMPLETE against activities.py rather
# than re-declaring a copy that would drift. An activity defined but missing from this list imports
# fine and fails at RUNTIME, when the workflow dispatches a name the worker never registered (#26).
ACTIVITIES = [
    call_agent_skill_activity,
    start_agent_skill_activity,
    publish_priority_activity,
    write_presign_impression_activity,
    escalate_activity,
    load_escalation_policy_activity,
    record_policy_failure_activity,
]


async def main() -> None:
    client = await _connect_with_retry(TEMPORAL_TARGET)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=ACTIVITIES,
    )
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
