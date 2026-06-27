"""StudyContext — the canonical lean envelope. Mirrors contracts/studycontext.schema.json.

If you change a field here, change the schema in the SAME PR (CI cross-checks fixtures).
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class Study(BaseModel):
    studyInstanceUID: str
    accessionNumber: Optional[str] = None
    orthancStudyId: str
    modality: str
    studyDescription: Optional[str] = None
    numberOfInstances: Optional[int] = None


class Patient(BaseModel):
    fhirPatientId: str
    openmrsPatientUuid: Optional[str] = None
    mrn: Optional[str] = None


class Order(BaseModel):
    fhirServiceRequestId: Optional[str] = None
    openmrsOrderUuid: Optional[str] = None
    priority: Optional[str] = None
    reasonCode: list[str] = Field(default_factory=list)


class Assignment(BaseModel):
    # Owned by LH-Radiology (specialty + case importance + call times). Read-only here.
    radiologistId: Optional[str] = None
    assignedAt: Optional[str] = None


class Meta(BaseModel):
    traceId: str
    spanId: Optional[str] = None
    emittedAt: str
    source: str
    schemaRef: Optional[str] = "studycontext@1.0.0"


class StudyContext(BaseModel):
    schemaVersion: str = "1.0.0"
    workflowId: str
    study: Study
    patient: Patient
    order: Order = Field(default_factory=Order)
    assignment: Assignment = Field(default_factory=Assignment)
    meta: Meta

    def model_dump_contract(self) -> dict:
        """Dump exactly as the wire/contract expects (exclude Nones to keep payloads lean)."""
        return self.model_dump(exclude_none=True)
