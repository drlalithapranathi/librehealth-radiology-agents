"""Fhir2Client.write_critical_result_notification (#79): in-EHR critical-result delivery.

Mocks the client's HTTP verbs (no live server), same idiom as the presign-write tests. What must
hold: the flag-off default performs NO I/O at all; the write is stamped with the dedicated
notification concept; a retried dispatch updates OUR obs for the SAME ack task instead of
duplicating; nothing clinician-authored is ever update-matched; and the transport guard refuses a
plaintext-remote base before any request goes out.
"""
from __future__ import annotations

import asyncio

import pytest

from radagent_common.fhir_client import (
    Fhir2Client,
    InsecureWriteTransportError,
    _ack_task_marker,
    ehr_inbox_write_enabled,
)

# Loopback so the write-transport guard always permits; the transport test overrides base_url.
_LOOPBACK_BASE = "http://localhost:8080/openmrs/ws/fhir2/R4"
# The provisioned default (drift-guarded against the bootstrap script in
# test_presign_concept_drift.py); hard-coded here so a silent default rotation fails a test.
_CONCEPT = "ea215431-5e85-5040-adf0-1da297c154c3"


def _client(existing_entries=None):
    """A Fhir2Client whose HTTP verbs record instead of transmitting. `existing_entries` is what
    the idempotency search finds on the patient."""
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    calls = {"get": [], "post": [], "put": []}

    async def fake_get(path, params=None):
        calls["get"].append((path, params))
        return {"entry": [{"resource": r} for r in (existing_entries or [])]}

    async def fake_post(path, resource):
        calls["post"].append((path, resource))
        return {"id": "obs-new-1"}

    async def fake_put(path, resource):
        calls["put"].append((path, resource))
        return {"id": resource["id"]}

    client._get = fake_get  # type: ignore[assignment]
    client._post = fake_post  # type: ignore[assignment]
    client._put = fake_put  # type: ignore[assignment]
    return client, calls


def _write(client, **overrides):
    kwargs = dict(
        patient_ref="Patient/pat-1",
        finding="pneumothorax", accession="ACC-CXR-001", ack_task_id="task-9",
        sent_iso="2026-07-19T12:00:00Z",
    )
    kwargs.update(overrides)
    return asyncio.run(client.write_critical_result_notification(**kwargs))


def _ours(accession="ACC-CXR-001", task_id="task-9", obs_id="obs-mine"):
    """An obs WE previously wrote: our concept stamp + the anchor segments in valueString."""
    segments = ["pneumothorax"]
    if accession:
        segments.append(f"accession {accession}")
    segments.append(_ack_task_marker(task_id))
    return {
        "resourceType": "Observation", "id": obs_id, "status": "final",
        "code": {"coding": [{"code": _CONCEPT}]},
        "valueString": " | ".join(segments),
    }


def test_flag_off_is_a_no_op_with_zero_io(monkeypatch):
    """Default off = INERT: not just "no write" but no request of any kind, so the merge changes
    nothing until a deployment flips the flag after the #79 sign-off."""
    monkeypatch.delenv("EHR_INBOX_WRITE_ENABLED", raising=False)
    client, calls = _client()
    assert _write(client) is None
    assert calls == {"get": [], "post": [], "put": []}


def test_flag_truthy_set_matches_the_other_write_gates(monkeypatch):
    """Same tokens as FHIR2_ALLOW_INSECURE_WRITE / ORTHANC_PRESIGN_WRITE_ENABLED (.strip().lower()
    in {1,true,yes}) -- write switches with different token sets are an operator trap (!73 item 3).
    """
    for token in ("1", "true", "YES", " True "):
        monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", token)
        assert ehr_inbox_write_enabled(), f"expected truthy: {token!r}"
    for token in ("", "0", "no", "off", "enabled"):
        monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", token)
        assert not ehr_inbox_write_enabled(), f"expected falsy: {token!r}"


def test_creates_the_stamped_observation(monkeypatch):
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client()
    obs_id = _write(client)

    assert obs_id == "obs-new-1"
    # Idempotency searches subject + code: subject alone pages past ours on a labs-heavy patient
    # (134 obs on the dev-stack probe patient, fhir2 pages at 10), and the concept-uuid code
    # filter is live-verified to narrow to exactly the stamped obs.
    assert calls["get"] == [("Observation", {"subject": "Patient/pat-1", "code": _CONCEPT})]
    ((path, posted),) = calls["post"]
    assert path == "Observation"
    # code must resolve to the provisioned Concept; a text-only code 500s on live fhir2.
    assert posted["code"]["coding"][0]["code"] == _CONCEPT
    assert posted["code"]["text"] == "AI critical result notification"
    assert posted["subject"] == {"reference": "Patient/pat-1"}
    # NO basedOn -- live fhir2 4.1.0 500s (translator NPE) on any Observation write carrying it
    # (bisected live 2026-07-19). Re-adding it means re-probing the deployed fhir2 first.
    assert "basedOn" not in posted
    assert posted["effectiveDateTime"] == "2026-07-19T12:00:00Z"
    assert posted["status"] == "final"
    # The chart entry is a POINTER: finding label + accession + ack correlation, no narrative.
    assert "pneumothorax" in posted["valueString"]
    assert "ACC-CXR-001" in posted["valueString"]
    assert _ack_task_marker("task-9") in posted["valueString"]
    assert "id" not in posted  # a create, not an update
    assert calls["put"] == []


def test_bare_ids_are_typed_before_writing(monkeypatch):
    """StudyContext envelopes may carry bare ids; the resource must still reference typed paths."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client()
    _write(client, patient_ref="pat-1")
    ((_, posted),) = calls["post"]
    assert posted["subject"] == {"reference": "Patient/pat-1"}


def test_dispatch_retry_updates_the_same_entry_and_points_it_at_the_live_ack_loop(monkeypatch):
    """THE retry that actually exists: a Temporal retry of the dispatch re-mints the ack Task, so
    the task id DIFFERS between attempts. Anchored on the accession, attempt 2 must UPDATE attempt
    1's entry in place -- one chart entry per critical result, now naming the newest (live) ack
    task -- never an accumulating pile. (Adversarial review caught the earlier task-id anchor
    failing exactly here.)"""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client(existing_entries=[_ours(task_id="task-from-attempt-1")])
    obs_id = _write(client, ack_task_id="task-from-attempt-2")
    assert obs_id == "obs-mine"
    assert calls["post"] == []
    ((path, put),) = calls["put"]
    assert path == "Observation/obs-mine"
    assert put["id"] == "obs-mine"
    assert _ack_task_marker("task-from-attempt-2") in put["valueString"].split(" | ")


def test_never_matches_an_obs_without_our_concept(monkeypatch):
    """AUTHORSHIP IS THE POINT: an obs not carrying our stamp -- a lab, a clinician-entered obs --
    is never update-matched, even if its text happens to contain our exact anchor segment."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    theirs = {
        "resourceType": "Observation", "id": "obs-theirs",
        "code": {"coding": [{"code": "5242-something-clinical"}]},
        "valueString": "note | accession ACC-CXR-001",
    }
    client, calls = _client(existing_entries=[theirs])
    _write(client)
    assert calls["put"] == []
    assert len(calls["post"]) == 1


def test_a_different_critical_results_notification_is_not_reused(monkeypatch):
    """Two critical results on one patient are two studies, two accessions, two chart entries:
    our OWN obs for a DIFFERENT accession must not be overwritten."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client(
        existing_entries=[_ours(accession="ACC-OTHER", task_id="task-1", obs_id="obs-other")])
    _write(client)
    assert calls["put"] == []
    assert len(calls["post"]) == 1


def test_prefix_colliding_accessions_do_not_cross_match(monkeypatch):
    """Anchor matching is an EXACT segment, never a substring: accession ACC-1 must not match a
    stored entry for ACC-11. (The adversarial-review finding: substring matching let one critical
    result's dispatch destroy another's chart entry.)"""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client(
        existing_entries=[_ours(accession="ACC-11", task_id="task-2", obs_id="obs-acc11")])
    _write(client, accession="ACC-1")
    assert calls["put"] == []
    assert len(calls["post"]) == 1


def test_prefix_colliding_task_ids_do_not_cross_match_on_the_fallback_anchor(monkeypatch):
    """No accession -> the anchor falls back to the exact ack-task segment. HAPI assigns
    sequential numeric Task ids, so "ack task 5" IS a substring of "ack task 52": task 5's write
    must POST its own entry, never PUT over task 52's."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client(
        existing_entries=[_ours(accession="", task_id="52", obs_id="obs-task-52")])
    _write(client, accession="", ack_task_id="5")
    assert calls["put"] == []
    assert len(calls["post"]) == 1


def test_uncorrelatable_notification_is_refused_before_any_io(monkeypatch):
    """No accession AND no ack task id: there is nothing to anchor on -- an empty-string anchor
    would match every entry (the id-less-Task clobber from adversarial review). Refuse loudly;
    the caller maps the raise to a FAILED channel result."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client, calls = _client(existing_entries=[_ours()])
    with pytest.raises(ValueError, match="uncorrelatable"):
        _write(client, accession="", ack_task_id="")
    assert calls == {"get": [], "post": [], "put": []}


def test_transport_guard_refuses_plaintext_remote_before_any_request(monkeypatch):
    """The refusal must come BEFORE the idempotency GET: a credentialed request must not leak to a
    plaintext remote host even when the write itself would be refused anyway."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    monkeypatch.delenv("FHIR2_ALLOW_INSECURE_WRITE", raising=False)
    client, calls = _client()
    client.base_url = "http://openmrs.example.org/openmrs/ws/fhir2/R4"
    with pytest.raises(InsecureWriteTransportError):
        _write(client)
    assert calls == {"get": [], "post": [], "put": []}


def test_concept_env_override_reaches_both_the_stamp_and_the_match(monkeypatch):
    """A deployment provisioning the concept at another UUID overrides one env var and both the
    write stamp and the idempotency match follow it."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    monkeypatch.setenv("FHIR2_CRITICAL_NOTIFICATION_CONCEPT", "deployment-specific-uuid")
    ours_elsewhere = dict(_ours(), code={"coding": [{"code": "deployment-specific-uuid"}]})
    client, calls = _client(existing_entries=[ours_elsewhere])
    _write(client)
    assert calls["post"] == []          # matched under the override...
    ((_, put),) = calls["put"]
    assert put["code"]["coding"][0]["code"] == "deployment-specific-uuid"   # ...and stamped with it


def test_ack_link_needs_base_url_and_secret_and_is_signed(monkeypatch):
    """The link appears only when the deployment configured BOTH the base URL and the HMAC secret
    -- an unsigned link is never minted, and no URL is invented that nothing serves. When both
    are set, the link carries this task's verifiable signature."""
    from radagent_common.ack_link import verify_ack_task

    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    monkeypatch.delenv("CRITCOM_ACK_BASE_URL", raising=False)
    monkeypatch.delenv("CRITCOM_ACK_HMAC_SECRET", raising=False)
    client, calls = _client()
    _write(client)
    ((_, plain),) = calls["post"]
    assert "ack link" not in plain["valueString"]

    monkeypatch.setenv("CRITCOM_ACK_BASE_URL", "https://demo.example.org/worklist/")
    client2, calls2 = _client()
    _write(client2)
    ((_, still_plain),) = calls2["post"]
    assert "ack link" not in still_plain["valueString"]     # base URL alone is not enough

    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", "test-secret")
    client3, calls3 = _client()
    _write(client3)
    ((_, linked),) = calls3["post"]
    assert "ack link: https://demo.example.org/worklist/ack/task-9?sig=" in linked["valueString"]
    sig = linked["valueString"].rsplit("?sig=", 1)[1].split(" | ")[0]
    assert verify_ack_task("task-9", sig, "test-secret")


def test_idempotency_search_follows_bundle_paging(monkeypatch):
    """The live miss this pins: fhir2 pages search results, and an obs on page 2 must still be
    found -- a paging miss silently duplicates chart entries on every dispatch retry."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    calls = {"get": [], "post": [], "put": []}
    page1 = {"entry": [],
             "link": [{"relation": "next", "url": "http://localhost:8080/fhir?page=2"}]}
    page2 = {"entry": [{"resource": _ours()}]}

    async def fake_get(path, params=None):
        calls["get"].append((path, params))
        return page2 if path.startswith("http") else page1

    async def fake_put(path, resource):
        calls["put"].append((path, resource))
        return {"id": resource["id"]}

    client._get = fake_get  # type: ignore[assignment]
    client._put = fake_put  # type: ignore[assignment]
    obs_id = _write(client)

    assert obs_id == "obs-mine"                       # found on page 2 -> updated, not duplicated
    assert calls["get"][1][0] == "http://localhost:8080/fhir?page=2"
    assert len(calls["put"]) == 1 and calls["post"] == []
