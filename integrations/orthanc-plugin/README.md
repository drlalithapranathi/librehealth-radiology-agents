# Orthanc plugin — OnStableStudy → orchestrator ingress

Owner: Parvati. Trigger map: [`ARCHITECTURE.md`](../../ARCHITECTURE.md). Contract:
[`contracts/events/orthanc-stable.schema.json`](../../contracts/events/orthanc-stable.schema.json).

When Orthanc marks a study *stable* (no new instances for `StableAge` seconds),
we fire a webhook at the orchestrator ingress. The ingress validates the payload
and starts one `StudyWorkflow` per study.

## Two ways to load it — pick one

Load **either** the Python plugin **or** the Lua fallback. Loading both would
send two webhooks per stable study and start two workflows for the same
`orthancStudyId` (Temporal would reject the duplicate id, but the log noise is
unnecessary).

### 1. Python plugin (primary)

- File: [`orthanc_stable_study.py`](./orthanc_stable_study.py)
- Requires the Orthanc Python plugin loaded in Orthanc.
  The `orthancteam/orthanc` image ships it built-in (v7.1+) and auto-enables it as
  soon as a `PythonScript` setting is present in the config.
- Uses `orthanc.RegisterOnChangeCallback` and filters for
  `orthanc.ChangeType.STABLE_STUDY` — the plugin's Python API does **not** expose
  a discrete `RegisterOnStableStudyCallback`, that name does not exist. Tags come
  from `orthanc.RestApiGet('/studies/{id}')` (the same source of truth the Lua
  fallback uses, so both paths emit a byte-identical payload — verified in
  `libs/radagent-common/tests/test_orthanc_stable_event.py::test_python_plugin_matches_lua_shape`).
- **Dev stack (already wired in `docker-compose.yml`)** — nothing to do, `docker
  compose up orthanc` mounts the script and sets `ORCH_WEBHOOK_URL` on the
  Orthanc container.
- **Manual deploy** — bind-mount the plugin file into the container and set:
  ```json
  {
    "PythonScript": "/etc/orthanc/orthanc_stable_study.py",
    "PythonVerbose": false
  }
  ```

### 2. Lua fallback (mitigates Risk R3)

- File: [`orthanc_stable_study.lua`](./orthanc_stable_study.lua)
- Use when the Python plugin is awkward to package (musl base images,
  air-gapped hosts, minimal Orthanc builds, or a Lua-only deployment policy).
- Emits the **identical** JSON body — the orchestrator ingress cannot tell
  which path fired.
- `orthanc.json`:
  ```json
  { "LuaScripts": ["/etc/orthanc/orthanc_stable_study.lua"] }
  ```

## Configuration (both paths)

| Env var             | Default                                               | Purpose                                         |
| ------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| `ORCH_WEBHOOK_URL`  | `http://orchestrator:8090/webhooks/orthanc`           | Where to POST the stable event. Must be http(s). |

Non-http(s) URLs are refused at runtime in both implementations (Bandit B310 / CWE-939).

## Payload contract

Both paths emit an `OrthancStableStudyEvent` v1.0.0. Example:

```json
{
  "schemaVersion":    "1.0.0",
  "eventType":        "orthanc.study.stable",
  "orthancStudyId":   "aorta-study-001",
  "studyInstanceUID": "1.2.840.113619.2.55.3.111111111",
  "modality":         "CT",
  "accessionNumber":  "ACC-AORTA-001",
  "occurredAt":       "2026-07-07T12:30:05Z"
}
```

Required: `schemaVersion`, `eventType`, `orthancStudyId`, `studyInstanceUID`,
`modality`, `occurredAt`. `accessionNumber` is optional (some scanners omit
it). See the schema for full constraints. The ingress rejects
non-conforming events with **422** — that's the CI gate.

`occurredAt` comes from Orthanc's study `LastUpdate`, which Orthanc reports in
DICOM datetime form (`YYYYMMDDTHHMMSS`, UTC) rather than RFC 3339, so both paths
reshape it to `YYYY-MM-DDTHH:MM:SSZ` to satisfy the schema's `format: date-time`.
When a build omits `LastUpdate`, both fall back to the current UTC time (e.g.
`2026-07-08T15:24:17Z`). `modality` is sourced from `ModalitiesInStudy`, which
Orthanc only returns when asked (`?requested-tags=ModalitiesInStudy`). Both
implementations use the same source and format, so the ingress cannot tell which
path fired.

## Lua deploy notes

- **`SetHttpHeaders` requires Orthanc 1.5.x+**. The Lua wraps that call in
  `pcall` so older builds still load the script; on those builds the HTTP
  client will infer `Content-Type: application/json` from the JSON body. If
  your ingress rejects requests without an explicit content-type, upgrade
  Orthanc or delete the `SetHttpHeaders` line.
- **`RestApiGet` is used to fetch the study record** rather than the
  callback's `tags`/`metadata` arguments. Reason: the delivery format of
  those callback args (pre-parsed table vs. JSON string) varies across
  Orthanc versions; the REST shape is stable across every build.
- **Errors are `print`ed to the Orthanc log** and swallowed. A downstream
  orchestrator outage must never take down the PACS.

## Smoke test (dev stack — Python primary path)

End-to-end proof that pushing a study fires the webhook and starts a workflow:

```bash
# 1. Bring up the minimum services for the trigger path.
docker compose up -d temporal-postgresql temporal orthanc orchestrator

# 2. In another terminal, tail the orchestrator ingress log.
docker compose logs -f orchestrator

# 3. Upload any DICOM instance to Orthanc via its REST API (no DICOM SCU needed).
#    Substitute your own .dcm; any single instance triggers a new study.
curl -X POST http://localhost:8042/instances --data-binary @sample.dcm

# 4. Wait StableAge seconds (default 60) — Orthanc marks the study stable, the
#    plugin fires OnChange(STABLE_STUDY), and POSTs the webhook.
```

What you should see in the orchestrator log:

```
INFO ... "POST /webhooks/orthanc HTTP/1.1" 200 OK
```

And in Temporal UI at http://localhost:8088, a new workflow with id
`wf_<orthancStudyId>` in the RUNNING state.

**Diagnosing failures**

- **HTTP 422 from the ingress** — the event failed schema validation. Check
  `docker compose logs orthanc` for the JSON the plugin sent, compare against
  `contracts/events/orthanc-stable.schema.json`.
- **Plugin never fires** — verify Python plugin loaded:
  `docker compose logs orthanc | grep -i python`. If the log shows
  `Registering plugin 'python'` but no OnChange calls, either `StableAge` hasn't
  elapsed yet or no `PythonScript` was found — check the config was mounted.
- **Plugin fires but no HTTP request** — `ORCH_WEBHOOK_URL` is malformed or
  points nowhere reachable from the orthanc container. The plugin refuses
  non-http(s) at runtime and logs `refusing non-HTTP(S) orchestrator webhook URL`.
