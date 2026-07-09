-- =============================================================================
-- LH-Radiology Orchestrator — OnStableStudy Lua fallback  (mitigates Risk R3)
-- =============================================================================
-- Purpose:
--   The Python plugin (orthanc_stable_study.py) is the primary path. This Lua
--   script is the drop-in fallback for deployments where packaging the Orthanc
--   Python plugin is awkward (musl base images, air-gapped hosts, minimal
--   Orthanc builds without the Python plugin baked in). It POSTs the
--   IDENTICAL webhook body to the orchestrator ingress.
--
-- IMPORTANT: load EITHER the Python plugin OR this Lua — never both, or the
-- ingress will receive duplicate stable events per study.
--
-- Contract:
--   Emits an OrthancStableStudyEvent v1.0.0
--   (contracts/events/orthanc-stable.schema.json). Field-for-field identical
--   to integrations/orthanc-plugin/orthanc_stable_study.py.
--
-- Deploy (see README.md in this directory for the full walkthrough):
--   1. Mount this file into the Orthanc container (e.g. /etc/orthanc/).
--   2. In orthanc.json:  "LuaScripts": ["/etc/orthanc/orthanc_stable_study.lua"]
--   3. Set env var ORCH_WEBHOOK_URL if the ingress isn't reachable at the
--      default http://orchestrator:8090/webhooks/orthanc.
--
-- Owner: Parvati.
-- =============================================================================

local DEFAULT_WEBHOOK = 'http://orchestrator:8090/webhooks/orthanc'

-- The Python plugin refuses non-http(s) URLs (Bandit B310 / CWE-939). Do the
-- same here so an accidental or tampered env var can never cause Orthanc to
-- dereference a file:// or ftp:// URL.
local function isHttpUrl(u)
  return type(u) == 'string'
     and (u:match('^http://') ~= nil or u:match('^https://') ~= nil)
end

local function resolveWebhook()
  local u = os.getenv('ORCH_WEBHOOK_URL')
  if u == nil or u == '' then u = DEFAULT_WEBHOOK end
  return u
end

-- RFC 3339 UTC "now". Used as a fall-through for occurredAt so the emitted
-- event is always schema-valid even if the study record is missing LastUpdate.
local function nowIsoUtc()
  return os.date('!%Y-%m-%dT%H:%M:%SZ')
end

-- Convert an Orthanc LastUpdate timestamp to RFC 3339, or nil if unparseable.
-- Orthanc reports LastUpdate as DICOM datetime 'YYYYMMDDTHHMMSS' (UTC), which is
-- NOT the schema's format: date-time (RFC 3339); reshape to 'YYYY-MM-DDTHH:MM:SSZ'.
-- A value already in RFC 3339 (has the date dashes) is passed through unchanged.
-- Mirrors the Python plugin's to_rfc3339_utc.
local function toRfc3339Utc(dt)
  if type(dt) ~= 'string' or dt == '' then return nil end
  local y, mo, d, h, mi, s = dt:match('^(%d%d%d%d)(%d%d)(%d%d)T(%d%d)(%d%d)(%d%d)$')
  if y then
    return string.format('%s-%s-%sT%s:%s:%sZ', y, mo, d, h, mi, s)
  end
  if dt:match('^%d%d%d%d%-%d%d%-%d%d') then return dt end  -- already RFC 3339
  return nil
end

-- Build the OrthancStableStudyEvent payload from a study record fetched via
-- the REST API. Sourcing tags via RestApiGet (rather than the callback's
-- tags/metadata arguments) is deliberately version-agnostic: how those args
-- are delivered to Lua callbacks varies across Orthanc builds, while the REST
-- shape is stable.
local function buildEvent(studyId, study)
  local mainTags      = (study and study.MainDicomTags) or {}
  -- ModalitiesInStudy is a computed study tag Orthanc only returns when explicitly
  -- requested (?requested-tags=), so it lands in RequestedTags, not MainDicomTags.
  -- Some builds also park AccessionNumber there.
  local requestedTags = (study and study.RequestedTags) or {}
  local accession     = mainTags.AccessionNumber or requestedTags.AccessionNumber or ''
  local modality      = requestedTags.ModalitiesInStudy or mainTags.ModalitiesInStudy
                        or mainTags.Modality or ''
  local studyUid      = mainTags.StudyInstanceUID or ''
  local occurredAt    = toRfc3339Utc(study and study.LastUpdate) or nowIsoUtc()

  return {
    schemaVersion    = '1.0.0',
    eventType        = 'orthanc.study.stable',
    orthancStudyId   = studyId,
    studyInstanceUID = studyUid,
    modality         = modality,
    accessionNumber  = accession,
    occurredAt       = occurredAt,
  }
end

-- Best-effort POST. Any error from HttpPost is swallowed by the surrounding
-- pcall in OnStableStudy — we must never raise out of the callback (would
-- fault the PACS on a downstream orchestrator outage).
local function postWebhook(url, payload)
  local body = DumpJson(payload, true)  -- true = keep string types (don't coerce)
  SetHttpTimeout(10)                    -- match the Python plugin's urlopen(timeout=10)
  -- SetHttpHeaders exists from Orthanc 1.5.x+. If your build is older, drop
  -- this line — Orthanc's Lua HTTP client will infer Content-Type from the
  -- JSON body. See README.md.
  pcall(function()
    SetHttpHeaders({ ['Content-Type'] = 'application/json' })
  end)
  HttpPost(url, body)
end

function OnStableStudy(studyId, tags, metadata)
  local webhook = resolveWebhook()
  if not isHttpUrl(webhook) then
    print('OnStableStudy: refusing non-http(s) webhook URL: ' .. tostring(webhook))
    return
  end

  local ok, study = pcall(function()
    -- ?requested-tags=ModalitiesInStudy makes Orthanc compute+return the study
    -- modality (absent from MainDicomTags by default). Same query as the Python path.
    return ParseJson(RestApiGet('/studies/' .. studyId .. '?requested-tags=ModalitiesInStudy'))
  end)
  if not ok or type(study) ~= 'table' then
    print('OnStableStudy: failed to read study ' .. tostring(studyId))
    return
  end

  local payload = buildEvent(studyId, study)

  local ok2, err = pcall(postWebhook, webhook, payload)
  if not ok2 then
    print('OnStableStudy: orchestrator webhook POST failed: ' .. tostring(err))
  end
end
