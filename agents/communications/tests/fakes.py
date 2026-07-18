"""In-memory doubles for the two stores the agent talks to (#52, MR 3).

Doubles rather than mocks: they hold state, so a test can dispatch, then check the ack, then
escalate, and assert on what actually ended up in the ledger. That is the only way to catch the
things that matter here -- an ack Task whose focus points at the wrong Communication, or an
escalation that leaves the original loop hanging open.
"""
from __future__ import annotations

from radagent_common.fhir_models import (
    CodeableConcept,
    Coding,
    Communication,
    PractitionerRole,
    Reference,
    ServiceRequest,
    Task,
    TaskStatus,
)


class FakeFhir2:
    """fhir2: read-only clinical context."""

    def __init__(self, requester: str | None = "Practitioner/dr-order"):
        self.requester = requester
        self.orders_read: list[str] = []

    async def get_service_request(self, ref: str):
        self.orders_read.append(ref)
        if not ref:
            return None
        return ServiceRequest(
            id=ref.split("/")[-1],
            subject=Reference(reference="Patient/1"),
            requester=Reference(reference=self.requester) if self.requester else None,
        )


class FakeLedger:
    """The comms ledger: Communication + Task + the on-call directory.

    The directory is one role: `on_call` is its practitioner, `on_call_specialty` its specialty
    tag (None = untagged, a general rota). `on_call_searches` records every specialty_code the
    agent searched with, so a test can assert the search really WAS narrowed (#58) -- not just
    that the right person happened to come back."""

    def __init__(self, on_call: str | None = "Practitioner/dr-oncall",
                 on_call_specialty: str | None = None):
        self.communications: dict[str, Communication] = {}
        self.tasks: dict[str, Task] = {}
        self.on_call = on_call
        self.on_call_specialty = on_call_specialty
        self.on_call_searches: list[str | None] = []
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    async def create_communication(self, comm: Communication) -> Communication:
        comm.id = self._next("comm")
        self.communications[comm.id] = comm
        return comm

    async def get_communication(self, resource_id: str) -> Communication:
        return self.communications[resource_id]

    async def create_task(self, task: Task) -> Task:
        task.id = self._next("task")
        self.tasks[task.id] = task
        return task

    async def get_task(self, resource_id: str) -> Task:
        return self.tasks[resource_id]

    async def update_task_status(self, resource_id: str, status: TaskStatus) -> Task:
        task = self.tasks[resource_id]
        task.status = status
        return task

    async def search_on_call_roles(self, specialty_code: str | None = None):
        """Mirrors the real search's contract: `specialty` is a server-side token filter, so a
        role with NO specialty tag does not match a narrowed search."""
        self.on_call_searches.append(specialty_code)
        if not self.on_call:
            return []
        if specialty_code and specialty_code != self.on_call_specialty:
            return []
        return [PractitionerRole(
            id="role-oncall",
            practitioner=Reference(reference=self.on_call),
            code=[CodeableConcept(coding=[Coding(code="on-call")])],
        )]
