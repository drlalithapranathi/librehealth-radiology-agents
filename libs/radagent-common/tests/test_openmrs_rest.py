"""OpenmrsRestClient transport guard (#67).

The REST surface (#70) rides the same wire as fhir2 -- same FHIR2_BASE_URL-derived host, same
Basic credentials, patient/order identifiers in the responses -- so it must obey the same
read-transport policy. These are the client's first tests; they pin the guard in all three
directions (refused / loopback-exempt / opted-in) with the same discipline as the fhir2 guard
tests: the refusal must happen BEFORE any request leaves the process.
"""
import asyncio
import os
from unittest import mock

import pytest

import radagent_common.openmrs_rest as openmrs_rest_module
from radagent_common.fhir_client import InsecureReadTransportError
from radagent_common.openmrs_rest import OpenmrsRestClient


class _RecordingClient:
    """Fake httpx.AsyncClient that fails the test if any request actually goes out."""

    attempts: list[str] = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _RecordingClient.attempts.append(url)
        raise AssertionError("the guard must refuse before any HTTP call")


class _OkClient:
    """Fake httpx.AsyncClient returning an empty result set."""

    requests: list[str] = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _OkClient.requests.append(url)

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

        return _R()


def _clear_opt_ins():
    os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
    os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)


def test_a_plaintext_remote_lookup_is_refused_before_any_http_call():
    """A remote plaintext hop carries the fhir2-unlocking credentials plus patient/order UUIDs in
    cleartext -- and ingress calls this on EVERY DICOM arrival (#70). Locking the fhir2 front door
    while this side door stays open would make the #67 guard theatre."""
    _RecordingClient.attempts = []
    with mock.patch.dict(os.environ, {}, clear=False):
        _clear_opt_ins()
        with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _RecordingClient):
            client = OpenmrsRestClient(base_url="http://remote-emr.example.org:8080/openmrs/ws/rest/v1")
            with pytest.raises(InsecureReadTransportError):
                asyncio.run(client.resolve_radiology_order_by_accession("ACC-1"))
    assert _RecordingClient.attempts == []


def test_loopback_plaintext_needs_no_opt_in():
    """localhost never leaves the machine; same exemption as the fhir2 guard."""
    _OkClient.requests = []
    with mock.patch.dict(os.environ, {}, clear=False):
        _clear_opt_ins()
        with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _OkClient):
            client = OpenmrsRestClient(base_url="http://localhost:8080/openmrs/ws/rest/v1")
            assert asyncio.run(client.resolve_radiology_order_by_accession("ACC-1")) is None
    assert len(_OkClient.requests) == 1


def test_the_read_opt_in_covers_the_rest_surface_too():
    """FHIR2_ALLOW_INSECURE_READ (and the FHIR2_ALLOW_INSECURE_WRITE inheritance, second case)
    must govern this client identically to fhir2 -- one wire, one policy, one opt-in."""
    for opt_in in ("FHIR2_ALLOW_INSECURE_READ", "FHIR2_ALLOW_INSECURE_WRITE"):
        _OkClient.requests = []
        with mock.patch.dict(os.environ, {}, clear=False):
            _clear_opt_ins()
            os.environ[opt_in] = "1"
            with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _OkClient):
                client = OpenmrsRestClient(
                    base_url="http://remote-emr.example.org:8080/openmrs/ws/rest/v1")
                assert asyncio.run(client.resolve_radiology_order_by_accession("ACC-1")) is None
        assert len(_OkClient.requests) == 1, f"request should have gone out under {opt_in}"
