"""Temporal worker: hosts StudyWorkflow + activities on the study task queue."""
from __future__ import annotations
import asyncio
import os
from temporalio.client import Client
from temporalio.worker import Worker

from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .activities import (
    call_agent_skill_activity,
    publish_priority_activity,
    escalate_activity,
)

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")


async def main() -> None:
    client = await Client.connect(TEMPORAL_TARGET)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=[
            call_agent_skill_activity,
            publish_priority_activity,
            escalate_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
