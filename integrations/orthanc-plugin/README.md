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
- `orthanc.json`:
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

## Smoke test (either path)

Push a study to Orthanc and confirm the ingress accepts it:

```bash
# in another terminal, tail the ingress log
docker compose logs -f orchestrator

# push a sample study
python -c "import pynetdicom; ..."   # or use storescu / OHIF upload
```

You should see `POST /webhooks/orthanc 200` and a new workflow started in
Temporal (`wf_<orthancStudyId>`). A 422 means the emitted event failed schema
validation — check `docker logs orthanc` for the payload the script sent.
