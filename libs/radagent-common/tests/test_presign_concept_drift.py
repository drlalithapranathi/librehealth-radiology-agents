"""Guard: the AI pre-sign impression draft concept UUID must not drift.

The UUID lives in three places (`fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT`,
`bootstrap_presign_concept.PRESIGN_CONCEPT_UUID`, and
`docker-compose.yml`'s `FHIR2_PRESIGN_REPORT_CONCEPT` env var on the
orchestrator). If any drifts, the orchestrator writes a DiagnosticReport
coded with a concept that was never provisioned. fhir2 500s on
`codeRequired`, the activity's bounded retry gives up,
`workflow._presign_impression` skips the draft -- and the draft silently
never appears. The read is never stranded, so nothing surfaces the
fault. That is the worst shape of failure for a write path into a chart.

This test is the guard. It re-derives the UUID from its documented seed
(`uuid5(uuid5(NAMESPACE_DNS, "librehealth.org"), "...v1")`) and asserts
all three copies agree, plus the two child UUIDs (name + description).
It lives in `ris-poller-tests` explicitly so CI actually runs it -- "a
guard CI never runs is not a guard" (Pranathi, in review of #55).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
import uuid
from pathlib import Path

# Repo root: this file is at libs/radagent-common/tests/test_presign_concept_drift.py
# so parents[3] is the repo root regardless of where pytest was invoked from.
REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_PATH = REPO_ROOT / "docker" / "openmrs" / "bootstrap_presign_concept.py"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

_SEED_NAMESPACE_DNS = "librehealth.org"
_SEED_CONCEPT = "lh-radiology.ai-presign-impression-draft.v1"


def _load_bootstrap_module():
    """Import the bootstrap script by path.

    The script lives under `docker/openmrs/` and is not part of any Python
    package -- it's mounted into the container at runtime and executed as a
    standalone script -- so we can't `import` it. Load it by file path.

    The script imports `pymysql` at module level, which isn't installed in
    `ris-poller-tests`. Stub it out just enough that the import succeeds; we
    only need the module-level constants, not any of the DB code.
    """
    if "pymysql" not in sys.modules:
        stub = types.ModuleType("pymysql")
        # `pymysql.Error` is referenced in exception handlers; `pymysql.connections`
        # is used only in a type annotation. Neither is called in this test.
        stub.Error = Exception
        stub.connections = types.SimpleNamespace(Connection=object)
        sys.modules["pymysql"] = stub

    spec = importlib.util.spec_from_file_location(
        "_bootstrap_for_drift_test", BOOTSTRAP_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_presign_concept_uuid_is_the_documented_seed():
    """The UUID in `fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT` must
    re-derive from the seed recorded in the bootstrap script's docstring
    (`uuid5(uuid5(NAMESPACE_DNS, 'librehealth.org'), '...v1')`).

    Guards against someone rotating the UUID without updating the seed
    docstring, or against a hand-typed value replacing the derived one.
    """
    from radagent_common import fhir_client

    ns = uuid.uuid5(uuid.NAMESPACE_DNS, _SEED_NAMESPACE_DNS)
    expected = str(uuid.uuid5(ns, _SEED_CONCEPT))
    assert expected == fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT, (
        f"documented-seed UUID5 derivation ({expected}) "
        f"does not match fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT "
        f"({fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT})"
    )


def test_bootstrap_script_uuid_agrees_with_the_client_default():
    """PRESIGN_CONCEPT_UUID in the bootstrap script must equal the client
    default. If they differ, the orchestrator writes a concept the script
    never provisioned -- silent codeRequired 500s at every pre-sign write.
    """
    from radagent_common import fhir_client

    bootstrap = _load_bootstrap_module()
    assert bootstrap.PRESIGN_CONCEPT_UUID == fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT, (
        f"bootstrap.PRESIGN_CONCEPT_UUID ({bootstrap.PRESIGN_CONCEPT_UUID}) "
        f"does not match fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT "
        f"({fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT})"
    )


def test_child_uuids_re_derive_from_the_documented_seeds():
    """Name and description UUIDs re-derive from the same seed pattern
    (with `.name.en` / `.description.en` suffixes). These only live in the
    bootstrap script -- nothing else references them -- but a drift would
    still be a bug: a rerun of the bootstrap after a UUID rotation would
    try to INSERT rows whose parent concept already exists, and mariadb
    would return a foreign-key or duplicate-key error.
    """
    bootstrap = _load_bootstrap_module()
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, _SEED_NAMESPACE_DNS)
    expected_name = str(uuid.uuid5(ns, _SEED_CONCEPT + ".name.en"))
    expected_desc = str(uuid.uuid5(ns, _SEED_CONCEPT + ".description.en"))
    assert bootstrap.PRESIGN_CONCEPT_NAME_UUID == expected_name, (
        f"bootstrap.PRESIGN_CONCEPT_NAME_UUID ({bootstrap.PRESIGN_CONCEPT_NAME_UUID}) "
        f"does not match documented-seed derivation ({expected_name})"
    )
    assert bootstrap.PRESIGN_CONCEPT_DESCRIPTION_UUID == expected_desc, (
        f"bootstrap.PRESIGN_CONCEPT_DESCRIPTION_UUID ({bootstrap.PRESIGN_CONCEPT_DESCRIPTION_UUID}) "
        f"does not match documented-seed derivation ({expected_desc})"
    )


def test_docker_compose_env_var_agrees_with_the_client_default():
    """FHIR2_PRESIGN_REPORT_CONCEPT in docker-compose.yml must equal the
    client default. If they differ, the env var overrides the default at
    runtime and the orchestrator writes a UUID the bootstrap never
    provisioned.

    Parsed with a strict regex rather than YAML: no new CI dep, and the
    line format is unambiguous by convention in this repo. If a future MR
    changes how the env var is expressed (e.g., quoted, split across
    lines), the regex fails loudly with 'env var not found' and the
    author is directed to update this test.
    """
    from radagent_common import fhir_client

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^\s+FHIR2_PRESIGN_REPORT_CONCEPT:\s*([0-9a-f-]{36})\s*$",
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        "FHIR2_PRESIGN_REPORT_CONCEPT env var not found in docker-compose.yml "
        "under the expected `      KEY: value` shape. If the env var was moved "
        "or reformatted, update the regex here."
    )
    compose_uuid = match.group(1)
    assert compose_uuid == fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT, (
        f"docker-compose.yml FHIR2_PRESIGN_REPORT_CONCEPT ({compose_uuid}) "
        f"does not match fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT "
        f"({fhir_client._DEFAULT_PRESIGN_REPORT_CONCEPT})"
    )


# --- #79: the critical-result notification concept, same three-way pin --------------------

_SEED_NOTIFICATION = "lh-radiology.ai-critical-result-notification.v1"


def test_notification_concept_uuid_is_the_documented_seed():
    """Same guard as the presign concept: the client default must re-derive from the documented
    seed, or a hand-typed value has replaced the derived one."""
    from radagent_common import fhir_client

    ns = uuid.uuid5(uuid.NAMESPACE_DNS, _SEED_NAMESPACE_DNS)
    expected = str(uuid.uuid5(ns, _SEED_NOTIFICATION))
    assert expected == fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT, (
        f"documented-seed UUID5 derivation ({expected}) "
        f"does not match fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT "
        f"({fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT})"
    )


def test_notification_bootstrap_uuid_agrees_with_the_client_default():
    """If these differ, the comms agent stamps an Observation with a concept the bootstrap never
    provisioned -- fhir2 refuses the write, the channel result reports FAILED on every critical
    dispatch, and the chart never sees a notification."""
    from radagent_common import fhir_client

    bootstrap = _load_bootstrap_module()
    assert bootstrap.NOTIFICATION_CONCEPT_UUID == fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT, (
        f"bootstrap.NOTIFICATION_CONCEPT_UUID ({bootstrap.NOTIFICATION_CONCEPT_UUID}) "
        f"does not match fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT "
        f"({fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT})"
    )


def test_notification_child_uuids_re_derive_from_the_documented_seeds():
    bootstrap = _load_bootstrap_module()
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, _SEED_NAMESPACE_DNS)
    expected_name = str(uuid.uuid5(ns, _SEED_NOTIFICATION + ".name.en"))
    expected_desc = str(uuid.uuid5(ns, _SEED_NOTIFICATION + ".description.en"))
    assert bootstrap.NOTIFICATION_CONCEPT_NAME_UUID == expected_name, (
        f"bootstrap.NOTIFICATION_CONCEPT_NAME_UUID ({bootstrap.NOTIFICATION_CONCEPT_NAME_UUID}) "
        f"does not match documented-seed derivation ({expected_name})"
    )
    assert bootstrap.NOTIFICATION_CONCEPT_DESCRIPTION_UUID == expected_desc, (
        f"bootstrap.NOTIFICATION_CONCEPT_DESCRIPTION_UUID "
        f"({bootstrap.NOTIFICATION_CONCEPT_DESCRIPTION_UUID}) "
        f"does not match documented-seed derivation ({expected_desc})"
    )


def test_notification_concept_datatype_is_text():
    """The notification Observation carries a valueString; fhir2 refuses an obs whose value does
    not match its concept's datatype. The bootstrap must therefore provision this concept with
    the Text datatype -- N/A (the presign stamp's datatype) would break every write."""
    bootstrap = _load_bootstrap_module()
    spec = next(s for s in bootstrap._CONCEPTS
                if s["uuid"] == bootstrap.NOTIFICATION_CONCEPT_UUID)
    assert spec["datatype_uuid"] == bootstrap.DATATYPE_TEXT_UUID, (
        "the notification concept must be provisioned with the Text datatype; "
        f"got {spec['datatype_uuid']}"
    )


def test_docker_compose_notification_env_var_agrees_with_the_client_default():
    """FHIR2_CRITICAL_NOTIFICATION_CONCEPT on the communications service must equal the client
    default, same three-way pin as the presign concept above."""
    from radagent_common import fhir_client

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^\s+FHIR2_CRITICAL_NOTIFICATION_CONCEPT:\s*([0-9a-f-]{36})\s*$",
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        "FHIR2_CRITICAL_NOTIFICATION_CONCEPT env var not found in docker-compose.yml "
        "under the expected `      KEY: value` shape. If the env var was moved "
        "or reformatted, update the regex here."
    )
    compose_uuid = match.group(1)
    assert compose_uuid == fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT, (
        f"docker-compose.yml FHIR2_CRITICAL_NOTIFICATION_CONCEPT ({compose_uuid}) "
        f"does not match fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT "
        f"({fhir_client._DEFAULT_CRITICAL_NOTIFICATION_CONCEPT})"
    )
