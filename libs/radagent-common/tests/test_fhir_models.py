"""The R4 models the Communications Agent reads and writes (#52, MR 2).

These resources are WRITTEN to a server, so the cost of a wrong field is a notification that
never reaches a physician. The tests below pin the three places where the Python shape and the
FHIR shape disagree and a naive port would silently break.
"""
from __future__ import annotations

from radagent_common.fhir_models import (
    Coding,
    CodeableConcept,
    ContactPoint,
    DiagnosticReport,
    Extension,
    HumanName,
    Practitioner,
    Reference,
    Task,
    TaskStatus,
)


def test_acr_category_url_is_a_classvar_not_a_serialized_field():
    """It is a constant, not data. Declared as a plain annotated attribute it would become a
    pydantic FIELD and ride along in every resource we POST to the server."""
    dumped = DiagnosticReport(id="r1").model_dump(mode="json", exclude_none=True)
    assert "ACR_CATEGORY_URL" not in dumped
    assert DiagnosticReport.ACR_CATEGORY_URL == "http://critcom/StructureDefinition/acr-category"


def test_acr_category_is_read_from_the_extension():
    report = DiagnosticReport(
        id="r1",
        extension=[Extension(url=DiagnosticReport.ACR_CATEGORY_URL, valueCode="Cat1")],
    )
    assert report.acr_category == "Cat1"
    assert DiagnosticReport(id="r2").acr_category is None      # absent extension -> None


def test_report_derives_its_order_and_patient_ids():
    report = DiagnosticReport(
        id="r1",
        subject=Reference(reference="Patient/p1"),
        basedOn=[Reference(reference="ServiceRequest/sr-1")],
    )
    assert report.service_request_id == "sr-1"
    assert report.patient_id == "p1"
    # A report with neither yields None rather than exploding — the agent degrades, not crashes.
    assert DiagnosticReport(id="r2").service_request_id is None
    assert DiagnosticReport(id="r2").patient_id is None


def test_task_for_is_aliased_and_populates_by_name():
    """`for` is a Python keyword. The model must accept BOTH the alias (parsing a server response)
    and the python name (constructing one in code), and must serialize as `for`."""
    from_server = Task.model_validate({"resourceType": "Task", "id": "t1", "status": "requested",
                                       "for": {"reference": "Patient/p1"}})
    assert from_server.for_ == Reference(reference="Patient/p1")

    in_code = Task(status=TaskStatus.REQUESTED, for_=Reference(reference="Patient/p1"))
    assert in_code.model_dump(mode="json", exclude_none=True, by_alias=True)["for"] == {
        "reference": "Patient/p1"}


def test_practitioner_contact_picks_the_requested_channel():
    p = Practitioner(
        id="dr-1",
        name=[HumanName(given=["Ada"], family="Lovelace")],
        telecom=[ContactPoint(system="email", value="ada@example.org"),
                 ContactPoint(system="pager", value="555-0199")],
    )
    assert p.display_name == "Ada Lovelace"
    assert p.contact("pager") == "555-0199"
    assert p.contact("phone") is None          # not reachable that way -> caller tries the next


def test_unknown_server_fields_do_not_break_parsing():
    """We model only the fields we touch. A real HAPI response carries plenty we don't — parsing
    must tolerate them rather than reject the resource."""
    report = DiagnosticReport.model_validate({
        "resourceType": "DiagnosticReport", "id": "r1", "status": "final",
        "conclusion": "Large pneumothorax.",
        "category": [{"coding": [{"code": "RAD"}]}],       # not modelled
        "effectiveDateTime": "2026-07-12T00:00:00Z",       # not modelled
    })
    assert report.conclusion == "Large pneumothorax."
    assert report.code is None


def test_codeable_concept_round_trips():
    cc = CodeableConcept(coding=[Coding(system="http://snomed.info/sct", code="on-call")],
                         text="On call")
    assert cc.model_dump(mode="json", exclude_none=True) == {
        "coding": [{"system": "http://snomed.info/sct", "code": "on-call"}], "text": "On call"}
