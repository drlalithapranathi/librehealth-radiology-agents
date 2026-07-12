"""FHIR R4 models — the subset the Communications Agent reads and writes (#52, MR 2).

Ported from CritCom's `src/critcom/fhir/models.py`, which models only the fields it actually
touches rather than the full spec. Kept as a separate module (not folded into fhir_client.py)
because the two stores speak the same resource vocabulary from opposite ends:

  * fhir2  — READ-ONLY clinical context: Patient, ServiceRequest, DiagnosticReport.
  * the comms ledger — READ/WRITE communication record: Communication, Task, and the
    Practitioner/PractitionerRole on-call directory (see comms_ledger.py for WHY it is separate).

Typed models rather than raw dicts because these resources are *written*: a Communication or a
Task with a mistyped status is a notification that never reaches a physician, and it fails at the
server, at 3am, rather than at the call site.

Lean-reference (golden rule 2) is unaffected: these are for talking to FHIR servers, not for
putting on the wire between agents. A2A messages still carry IDs only.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

# --- shared primitives -------------------------------------------------------------


class Coding(BaseModel):
    system: str | None = None
    code: str | None = None
    display: str | None = None


class CodeableConcept(BaseModel):
    coding: list[Coding] = Field(default_factory=list)
    text: str | None = None


class Reference(BaseModel):
    reference: str | None = None  # e.g. "Practitioner/123"
    display: str | None = None


class ContactPoint(BaseModel):
    system: str | None = None   # phone | fax | email | pager | url | sms | other
    value: str | None = None
    use: str | None = None      # home | work | temp | old | mobile


class HumanName(BaseModel):
    use: str | None = None
    family: str | None = None
    given: list[str] = Field(default_factory=list)

    @property
    def display(self) -> str:
        return " ".join(self.given + ([self.family] if self.family else []))


class Period(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


class Meta(BaseModel):
    versionId: str | None = None
    lastUpdated: datetime | None = None


class Extension(BaseModel):
    url: str
    valueString: str | None = None
    valueCode: str | None = None


# --- status value sets -------------------------------------------------------------


class TaskStatus(str, Enum):
    DRAFT = "draft"
    REQUESTED = "requested"
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    READY = "ready"
    CANCELLED = "cancelled"
    IN_PROGRESS = "in-progress"
    ON_HOLD = "on-hold"
    FAILED = "failed"
    COMPLETED = "completed"
    ENTERED_IN_ERROR = "entered-in-error"


class CommunicationStatus(str, Enum):
    PREPARATION = "preparation"
    IN_PROGRESS = "in-progress"
    NOT_DONE = "not-done"
    ON_HOLD = "on-hold"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ENTERED_IN_ERROR = "entered-in-error"
    UNKNOWN = "unknown"


class ServiceRequestStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ON_HOLD = "on-hold"
    REVOKED = "revoked"
    COMPLETED = "completed"
    ENTERED_IN_ERROR = "entered-in-error"
    UNKNOWN = "unknown"


class DiagnosticReportStatus(str, Enum):
    REGISTERED = "registered"
    PARTIAL = "partial"
    PRELIMINARY = "preliminary"
    FINAL = "final"
    AMENDED = "amended"
    CORRECTED = "corrected"
    APPENDED = "appended"
    CANCELLED = "cancelled"
    ENTERED_IN_ERROR = "entered-in-error"
    UNKNOWN = "unknown"


# --- on-call directory (comms ledger) ----------------------------------------------


class Practitioner(BaseModel):
    resourceType: str = "Practitioner"
    id: str | None = None
    meta: Meta | None = None
    name: list[HumanName] = Field(default_factory=list)
    telecom: list[ContactPoint] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name[0].display if self.name else f"Practitioner/{self.id}"

    def contact(self, system: str) -> str | None:
        """First contact value for a system ('phone', 'pager', 'email'), or None."""
        return next((cp.value for cp in self.telecom if cp.system == system and cp.value), None)


class PractitionerRole(BaseModel):
    resourceType: str = "PractitionerRole"
    id: str | None = None
    meta: Meta | None = None
    active: bool = True
    period: Period | None = None
    practitioner: Reference | None = None
    organization: Reference | None = None
    code: list[CodeableConcept] = Field(default_factory=list)
    telecom: list[ContactPoint] = Field(default_factory=list)

    def contact(self, system: str) -> str | None:
        return next((cp.value for cp in self.telecom if cp.system == system and cp.value), None)


# --- clinical context (read-only, from fhir2) ---------------------------------------


class Patient(BaseModel):
    resourceType: str = "Patient"
    id: str | None = None
    meta: Meta | None = None
    name: list[HumanName] = Field(default_factory=list)
    birthDate: str | None = None
    gender: str | None = None

    @property
    def display_name(self) -> str:
        return self.name[0].display if self.name else f"Patient/{self.id}"


class ServiceRequest(BaseModel):
    resourceType: str = "ServiceRequest"
    id: str | None = None
    meta: Meta | None = None
    status: ServiceRequestStatus = ServiceRequestStatus.ACTIVE
    intent: str = "order"
    priority: str = "routine"                                # routine | urgent | asap | stat
    code: CodeableConcept | None = None
    subject: Reference | None = None                         # -> Patient
    requester: Reference | None = None                       # -> Practitioner / PractitionerRole
    performer: list[Reference] = Field(default_factory=list)
    reasonCode: list[CodeableConcept] = Field(default_factory=list)
    note: list[dict[str, Any]] = Field(default_factory=list)


class DiagnosticReport(BaseModel):
    resourceType: str = "DiagnosticReport"
    id: str | None = None
    meta: Meta | None = None
    status: DiagnosticReportStatus = DiagnosticReportStatus.FINAL
    code: CodeableConcept | None = None
    subject: Reference | None = None                         # -> Patient
    basedOn: list[Reference] = Field(default_factory=list)   # -> ServiceRequest
    issued: datetime | None = None
    performer: list[Reference] = Field(default_factory=list)
    conclusion: str | None = None
    presentedForm: list[dict[str, Any]] = Field(default_factory=list)
    extension: list[Extension] = Field(default_factory=list)

    # ClassVar, NOT a field: a plain annotated attribute would become a pydantic field and get
    # serialized into every resource we POST.
    ACR_CATEGORY_URL: ClassVar[str] = "http://critcom/StructureDefinition/acr-category"

    @property
    def acr_category(self) -> str | None:
        """The ACR urgency category (Cat1/Cat2/Cat3) if the report carries the extension."""
        for ext in self.extension:
            if ext.url == self.ACR_CATEGORY_URL:
                return ext.valueCode or ext.valueString
        return None

    @property
    def service_request_id(self) -> str | None:
        for ref in self.basedOn:
            if ref.reference and ref.reference.startswith("ServiceRequest/"):
                return ref.reference.split("/", 1)[1]
        return None

    @property
    def patient_id(self) -> str | None:
        ref = self.subject.reference if self.subject else None
        return ref.split("/", 1)[1] if ref and ref.startswith("Patient/") else None


# --- the communication record (written to the comms ledger) --------------------------


class CommunicationPayload(BaseModel):
    contentString: str | None = None


class Communication(BaseModel):
    """"We told someone." The durable record that a critical result was communicated."""

    resourceType: str = "Communication"
    id: str | None = None
    meta: Meta | None = None
    status: CommunicationStatus = CommunicationStatus.IN_PROGRESS
    category: list[CodeableConcept] = Field(default_factory=list)
    subject: Reference | None = None                         # -> Patient
    # R4 distinguishes basedOn (the request this fulfils; searchable as `based-on`) from about
    # (topical refs; NOT a default HAPI search param). Both carry the originating ServiceRequest
    # so the audit query can search by `based-on` while clients still see the topical link.
    basedOn: list[Reference] = Field(default_factory=list)   # -> ServiceRequest
    about: list[Reference] = Field(default_factory=list)     # -> ServiceRequest
    recipient: list[Reference] = Field(default_factory=list)
    sender: Reference | None = None
    sent: datetime | None = None
    payload: list[CommunicationPayload] = Field(default_factory=list)
    note: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def finding_summary(self) -> str | None:
        return self.payload[0].contentString if self.payload else None


class TaskRestriction(BaseModel):
    repetitions: int | None = None
    period: Period | None = None                             # the ack deadline lives here
    recipient: list[Reference] = Field(default_factory=list)


class Task(BaseModel):
    """"Did they acknowledge?" The open loop on a Communication, closed by an ack."""

    resourceType: str = "Task"
    id: str | None = None
    meta: Meta | None = None
    status: TaskStatus = TaskStatus.REQUESTED
    intent: str = "order"
    priority: str = "routine"                                # routine | urgent | asap | stat
    code: CodeableConcept | None = None
    focus: Reference | None = None                           # -> Communication
    for_: Reference | None = Field(default=None, alias="for")  # -> Patient ('for' is a keyword)
    authoredOn: datetime | None = None
    lastModified: datetime | None = None
    requester: Reference | None = None
    owner: Reference | None = None                           # -> Practitioner expected to ack
    restriction: TaskRestriction | None = None
    note: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# --- search results ------------------------------------------------------------------


class BundleEntry(BaseModel):
    fullUrl: str | None = None
    resource: dict[str, Any] | None = None


class Bundle(BaseModel):
    resourceType: str = "Bundle"
    id: str | None = None
    type: str = "searchset"
    total: int = 0
    entry: list[BundleEntry] = Field(default_factory=list)
