"""CritCom's clinical tools, ported onto the two-store split (#52, MR 3). Owner: Pranathi (lead).

CritCom's originals each open their own FHIR client against ONE server. Ours cannot: clinical
context is read from fhir2 (read-only) and the communication record is written to the comms
ledger (see radagent_common/comms_ledger.py for why). So every tool here takes its clients as
arguments -- which also makes them trivially fake-able in tests, with no network and no monkey-
patching of module globals.

ONE DELIBERATE DEVIATION FROM CRITCOM, and it is the important one:

CritCom resolves the ordering physician by dereferencing `ServiceRequest.requester` into a
`Practitioner` on the same server. We do NOT dereference it. The order lives in fhir2 and the
on-call directory lives in the ledger, so following that reference would mean reading a
practitioner out of fhir2 and hoping the ids line up across two stores. Instead the requester
reference is carried VERBATIM onto `Communication.recipient` and `Task.owner`: a FHIR reference is
already a stable identifier, and nothing in v1 needs the practitioner's name or phone number --
recording who was notified does not require dialling them. Real delivery (M3) is where contact
details are needed, and by then the deployment has told us which directory to dial from.

The on-call provider is different: `PractitionerRole` does not exist in fhir2 at any version, so
the on-call directory is ledger-native and IS dereferenced there.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from radagent_common.comms_ledger import CommsLedgerClient
from radagent_common.fhir_client import Fhir2Client
from radagent_common.fhir_models import (
    CodeableConcept,
    Coding,
    Communication,
    CommunicationPayload,
    CommunicationStatus,
    Period,
    Reference,
    Task,
    TaskRestriction,
    TaskStatus,
)

from routing import FALLBACK_ANY_ON_CALL, FALLBACK_NONE

_log = logging.getLogger("agents.communications.tools")

_ACR_SYSTEM = "http://critcom/acr-category"
_TASK_TYPE_SYSTEM = "http://critcom/task-type"
_ACK_TASK_CODE = "critical-result-ack"
_ROUTING_SYSTEM = "http://critcom/routing"
OUT_OF_SPECIALTY_CODE = "out-of-specialty"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def resolve_ordering_provider(fhir: Fhir2Client, service_request_ref: str) -> str | None:
    """The reference of the physician who ordered the study -- our notification recipient.

    Returned as an opaque FHIR reference (e.g. 'Practitioner/123'), NOT dereferenced; see the
    module docstring. None when the order is missing or carries no requester, which is a real
    outcome on a study ingested with an unresolved order (#11) -- the caller falls back to on-call
    rather than dropping the notification.
    """
    if not service_request_ref:
        return None
    order = await fhir.get_service_request(service_request_ref)
    if order is None or order.requester is None:
        return None
    return order.requester.reference


@dataclass(frozen=True)
class OnCallResolution:
    """Who the on-call search found, and whether they were the second choice.

    `reference` is None when nobody eligible is on call. `out_of_specialty` is True only when a
    specialty was requested, nobody tagged for it was on call, and the any-on-call fallback found
    someone else -- the fact the dispatch record must carry (#58)."""
    reference: str | None
    out_of_specialty: bool = False


def _first_practitioner(roles) -> str | None:
    for role in roles:
        if role.practitioner and role.practitioner.reference:
            return role.practitioner.reference
    return None


async def resolve_on_call_provider(
    ledger: CommsLedgerClient,
    *,
    specialty: str | None = None,
    fallback: str = FALLBACK_ANY_ON_CALL,
) -> OnCallResolution:
    """Whoever is on call right now, from the ledger's directory.

    With a `specialty` the search is narrowed to roles tagged for it (#58): a critical
    intracranial finding should page neuro call, not whoever the directory lists first. When
    nobody in that specialty is on call, `fallback` picks the failure direction --
    FALLBACK_ANY_ON_CALL re-searches unnarrowed and marks the result out-of-specialty;
    FALLBACK_NONE resolves nobody, so the caller reports the miss honestly instead of paging out
    of specialty. Nothing downstream re-pages in response to that miss: the sign-off ladder
    (#29) is the pre-sign gate, decoupled from dispatch, so under FALLBACK_NONE a miss is only
    as loud as its ledger/log record.

    A None reference is a real answer, not an error: the caller must surface it (an
    unescalatable critical result is exactly the thing a human has to hear about) rather than
    treat an empty directory as a delivered page.
    """
    if specialty:
        ref = _first_practitioner(await ledger.search_on_call_roles(specialty_code=specialty))
        if ref:
            return OnCallResolution(ref)
        if fallback == FALLBACK_NONE:
            return OnCallResolution(None)
        ref = _first_practitioner(await ledger.search_on_call_roles())
        return OnCallResolution(ref, out_of_specialty=ref is not None)
    return OnCallResolution(_first_practitioner(await ledger.search_on_call_roles()))


async def dispatch_communication(
    ledger: CommsLedgerClient,
    *,
    patient_ref: str,
    service_request_ref: str,
    recipient_ref: str,
    acr_category: str,
    finding: str,
    out_of_specialty: bool = False,
) -> Communication:
    """Record that a critical result was communicated. This is the durable "we told someone".

    `out_of_specialty` stamps the record when the recipient was the any-on-call fallback rather
    than the study's own specialty rota (#58) -- the audit trail must say the page landed on the
    wrong someone, or a chart review reads it as a properly-routed communication."""
    category = [CodeableConcept(
        coding=[Coding(system=_ACR_SYSTEM, code=acr_category)], text=acr_category)]
    if out_of_specialty:
        # Appended, never first: readers (including _escalate's category re-derive) take the ACR
        # category from category[0].
        category.append(CodeableConcept(
            coding=[Coding(system=_ROUTING_SYSTEM, code=OUT_OF_SPECIALTY_CODE)],
            text="recipient was not on call for the study's specialty"))
    comm = Communication(
        status=CommunicationStatus.IN_PROGRESS,
        category=category,
        subject=Reference(reference=patient_ref),
        # basedOn is the searchable link (`based-on`); about is the topical twin. See fhir_models.
        basedOn=[Reference(reference=service_request_ref)],
        about=[Reference(reference=service_request_ref)],
        recipient=[Reference(reference=recipient_ref)],
        sent=_now(),
        payload=[CommunicationPayload(contentString=finding)],
    )
    created = await ledger.create_communication(comm)
    _log.info("communication %s recorded for %s (%s)", created.id, service_request_ref, acr_category)
    return created


async def open_ack_task(
    ledger: CommsLedgerClient,
    *,
    communication_ref: str,
    patient_ref: str,
    owner_ref: str,
    ack_minutes: int,
) -> tuple[Task, datetime]:
    """Open the ack clock: a Task the recipient must complete, with the deadline on its
    restriction period. Returns (task, deadline) -- the orchestrator holds the durable timer
    against that deadline (MR 4); this agent never self-fires."""
    now = _now()
    deadline = now + timedelta(minutes=ack_minutes)
    task = Task(
        status=TaskStatus.REQUESTED,
        intent="order",
        priority="stat",
        code=CodeableConcept(
            coding=[Coding(system=_TASK_TYPE_SYSTEM, code=_ACK_TASK_CODE)],
            text="Critical result acknowledgment"),
        focus=Reference(reference=communication_ref),
        for_=Reference(reference=patient_ref),
        authoredOn=now,
        lastModified=now,
        owner=Reference(reference=owner_ref),
        restriction=TaskRestriction(repetitions=1, period=Period(start=now, end=deadline)),
    )
    created = await ledger.create_task(task)
    _log.info("ack task %s opened, due %s", created.id, deadline.isoformat())
    return created, deadline


def ack_state(task: Task, now: datetime | None = None) -> tuple[str, datetime | None, bool]:
    """(ackStatus, deadline, overdue) for a Task -- the comms.checkAck contract's three fields.

    ACCEPTED counts as acknowledged alongside COMPLETED: a physician whose system marks the task
    accepted has seen it, and treating that as unacknowledged would escalate a result that already
    landed. A task past its deadline that is neither is OVERDUE -- the trigger for comms.escalate.
    """
    now = now or _now()
    period = task.restriction.period if task.restriction else None
    deadline = period.end if period else None
    acknowledged = task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED)
    if acknowledged:
        return "COMPLETED", deadline, False
    overdue = deadline is not None and now > deadline
    return ("OVERDUE" if overdue else "REQUESTED"), deadline, overdue


async def escalate_to_on_call(
    ledger: CommsLedgerClient,
    *,
    overdue_task_id: str,
    patient_ref: str,
    service_request_ref: str,
    acr_category: str,
    finding: str,
    ack_minutes: int,
    specialty: str | None = None,
    fallback: str = FALLBACK_ANY_ON_CALL,
) -> dict:
    """Nobody acknowledged. Fail the open loop, find the on-call provider, and open a new one.

    The original Task is marked FAILED rather than left hanging, so the ledger says plainly that
    this loop was never closed by its intended recipient -- that is the audit fact a chart review
    needs. `specialty`/`fallback` route the on-call search the same way dispatch's fallback does
    (#58). Returns a dict shaped for the comms.escalate contract.
    """
    await ledger.update_task_status(overdue_task_id, TaskStatus.FAILED)

    resolution = await resolve_on_call_provider(ledger, specialty=specialty, fallback=fallback)
    on_call_ref = resolution.reference
    if not on_call_ref:
        # An unescalatable critical result. Say so loudly and truthfully rather than reporting a
        # page nobody received -- and say WHICH miss it was: "the policy chose not to page out of
        # specialty" and "the directory is empty" are different audit facts.
        if specialty and fallback == FALLBACK_NONE:
            reason = (f"nobody is on call for '{specialty}' and the routing policy "
                      "(outOfSpecialtyFallback: none) does not page out of specialty")
        else:
            reason = "no on-call provider is configured in the ledger"
        _log.error("%s; %s cannot be escalated", reason, overdue_task_id)
        return {"escalated": False, "reason": reason}

    comm = await dispatch_communication(
        ledger,
        patient_ref=patient_ref,
        service_request_ref=service_request_ref,
        recipient_ref=on_call_ref,
        acr_category=acr_category,
        finding=f"[ESCALATED] {finding}",
        out_of_specialty=resolution.out_of_specialty,
    )
    task, deadline = await open_ack_task(
        ledger,
        communication_ref=f"Communication/{comm.id}",
        patient_ref=patient_ref,
        owner_ref=on_call_ref,
        ack_minutes=ack_minutes,
    )
    reason = f"no acknowledgement before the deadline; escalated to {on_call_ref}"
    if resolution.out_of_specialty:
        reason += f" (out of specialty: nobody on call for '{specialty}')"
    return {
        "escalated": True,
        "newCommunicationId": comm.id or "",
        "newTaskId": task.id or "",
        "newDeadline": deadline.isoformat(),
        "reason": reason,
    }
