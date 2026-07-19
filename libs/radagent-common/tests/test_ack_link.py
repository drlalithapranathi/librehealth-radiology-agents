"""radagent_common.ack_link (#79): the HMAC on the acknowledgement link.

What must hold: sign/verify round-trips; a signature never validates for a different task; an
unconfigured deployment fails CLOSED (verify False, sign raises); empty inputs never validate.
"""
from __future__ import annotations

import pytest

from radagent_common.ack_link import ack_secret, sign_ack_task, verify_ack_task

_SECRET = "unit-test-secret"


def test_sign_verify_round_trip():
    sig = sign_ack_task("task-42", _SECRET)
    assert verify_ack_task("task-42", sig, _SECRET)


def test_signature_is_bound_to_the_task_id():
    """task-42's link must not acknowledge task-43 -- and (the HAPI sequential-id trap again)
    task-4's signature must not validate for task-42."""
    sig = sign_ack_task("task-4", _SECRET)
    assert not verify_ack_task("task-42", sig, _SECRET)
    assert not verify_ack_task("task-43", sign_ack_task("task-42", _SECRET), _SECRET)


def test_tampered_signature_fails():
    sig = sign_ack_task("task-42", _SECRET)
    tampered = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert not verify_ack_task("task-42", tampered, _SECRET)


def test_wrong_secret_fails():
    assert not verify_ack_task("task-42", sign_ack_task("task-42", _SECRET), "other-secret")


def test_unconfigured_deployment_fails_closed(monkeypatch):
    """No CRITCOM_ACK_HMAC_SECRET: signing raises (an unsigned link is never minted) and
    verification is False for ANY signature (the surface does not exist)."""
    monkeypatch.delenv("CRITCOM_ACK_HMAC_SECRET", raising=False)
    assert ack_secret() == ""
    with pytest.raises(ValueError, match="no ack-link secret"):
        sign_ack_task("task-42")
    assert not verify_ack_task("task-42", "any-signature")


def test_env_secret_is_used_when_none_passed(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    assert verify_ack_task("task-42", sign_ack_task("task-42"))


def test_empty_inputs_never_validate():
    with pytest.raises(ValueError, match="empty ack task id"):
        sign_ack_task("", _SECRET)
    assert not verify_ack_task("", "sig", _SECRET)
    assert not verify_ack_task("task-42", "", _SECRET)
