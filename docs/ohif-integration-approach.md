# OHIF integration approach — R2 risk decision

Owner: Parvati. Issue: #4 (`Risk R2`). Status: **decision recorded, execution folds into #21.**

R2 was framed as: *the OHIF custom worklist data source might be too much effort;
fall back to Orthanc DICOMweb if so.* This document walks the three approaches
against #21's three M2 requirements (data source, priors/overlays,
`StudyOpenedEvent`), picks one, and calls out the consequences so #21 can
proceed without re-litigating the scope.

## What #21 needs OHIF to do

1. **Reading list ordered by orchestrator priority.** Rows come from our
   Worklist API (#20 landed) — a JSON list already sorted STAT → URGENT →
   ROUTINE, annotated with assignment. OHIF's built-in Study List sorts by
   `StudyDate` desc; there is no DICOM search parameter for "priority" and
   never will be, because priority lives outside DICOM.
2. **Priors and overlays surfaced at read time.** Priors come from the EHR
   Assistant's context packet (#4). Overlays reference Interpretation
   Assistant SC output. Both need to appear as panels alongside the images.
3. **`StudyOpenedEvent` emission** when a radiologist opens a study.
   Contract: `contracts/events/ohif-opened.schema.json`. Enables read-time
   pre-sign assist. Fired once per opened study.

## Approaches considered

OHIF v3's plugin architecture offers three levels of customization, cleanly
separated by which OHIF surface you replace. I've mapped each to what #21
needs.

### Approach A — full custom data source (the risk R2 originally flagged)

Implement `IWebApiDataSource` from `@ohif/core` as the data source module of a
new extension. The custom data source becomes what OHIF asks for studies,
series, and instance metadata; the DICOMweb call to Orthanc goes through
*our* code, which can inject priority and re-order results before returning
them to OHIF.

* **What it buys:** replaces the study list at the source, so OHIF's built-in
  Study List UI works unchanged and users see priority ordering natively.
* **What it costs:** implementing the full data source contract
  (`retrieve.series.metadata`, `retrieve.studies`, `retrieve.study.metadata`,
  `store.dicom`, `deleteStudy`, plus the naturalized-DICOM-JSON mapping into
  OHIF's `DicomMetadataStore`). Every method that touches DICOM has to be
  re-implemented; skipping any breaks OHIF's assumption that a data source is
  a complete DICOMweb backend. Priors/overlays and `StudyOpenedEvent` are
  *additional* extension work on top of this.
* **Rough effort:** 2–4 weeks for a solid v1. Debugging surface is large —
  many bugs surface only when a study of a particular SOP class loads.
* **Verdict:** This is what R2 correctly identified as "heavy." Not
  recommended.

### Approach B — custom mode + custom `WorkList` component (reuse built-in DICOMweb data source)

Build an extension that provides:

* A **custom mode** registered at `/reading` (or similar) with a
  `layoutTemplate` whose `component` is our own React `WorkList` that
  fetches from `GET /worklist` on our Worklist API and renders priority-ordered
  rows. OHIF docs [confirm this is the supported way](https://docs.ohif.org/platform/modes/routes/)
  to replace the WorkList UI.
* **Clicking a row opens the study in OHIF's normal viewer** via the
  built-in `dicomweb` data source pointed at Orthanc — no data source
  override; the Orthanc DICOMweb pipeline we already run in `docker-compose`
  keeps handling the imaging.
* A **left/right panel** registered via `getPanelModule` that reads priors
  from a query param (`?priorsRef=…`) or from a small companion API call and
  renders them.
* **`StudyOpenedEvent`** emitted from the mode's `route.init` or
  `onModeEnter` when the study route is entered — a few lines of `fetch`
  against an ingest endpoint (existing `orchestrator/ingress` shape or a
  small addition).

* **What it buys:** all three #21 requirements, no full-data-source work,
  DICOMweb pipeline unchanged, no risk to imaging performance.
* **What it costs:** a real extension repo, a real React component, real
  OHIF-webpack build. Non-trivial but bounded.
* **Rough effort:** 1–2 weeks for #21's three requirements.
* **Verdict:** **This is the recommended path.** It buys what #21 needs at a
  fraction of Approach A's cost.

### Approach C — no OHIF extension at all, external worklist page

Skip OHIF's WorkList entirely. Serve our own React worklist at a
non-OHIF route (`nginx` proxies `/reading` to a tiny sidecar). Clicking a
study builds an OHIF viewer URL and navigates. `StudyOpenedEvent` fires from
the sidecar on click.

* **What it buys:** no OHIF extension, no OHIF-webpack build, no OHIF learning
  curve. The lightest possible integration.
* **What it costs:** priors/overlays cannot appear in OHIF (they'd have to be
  a companion panel outside the viewer, which is a poor read-time
  experience). The `StudyOpenedEvent` fires when the *link is clicked*, not
  when the study actually opens — the radiologist can be interrupted before
  the viewer loads and we've already emitted the event.
* **Rough effort:** a few days.
* **Verdict:** Sacrifices requirement 2 (priors/overlays at read time). Not
  recommended as the primary path.

## Decision

**Approach B — custom mode + custom WorkList component, reusing OHIF's
built-in DICOMweb data source pointed at Orthanc.**

This is *not* the "custom data source" R2 warned about — it deliberately
avoids re-implementing DICOMweb. It is also *not* a fallback to plain Orthanc
DICOMweb; that would abandon priority ordering, priors, overlays, and
`StudyOpenedEvent` all at once, which forfeits the point of M2.

R2's mitigation language ("fall back to an Orthanc DICOMweb data source if
the custom extension is heavy") should be read as "avoid Approach A"; the
Approach B path is what R2 was implicitly pointing at as the sane middle.

## Consequences for #21

* **Skeleton is `integrations/ohif-extension/`** — the placeholder already
  exists in the repo (`README.md` there is the current stub).
* **Extension type: OHIF v3 extension + mode**, packaged as a Node/TS
  workspace. Registered via `window.config.extensions` and
  `window.config.modes` in `docker/ohif/app-config.js` (currently `[]`).
* **`docker/ohif/app-config.js` change is deferred** to the #21 MR itself so
  the extension is only registered once it exists (avoids a broken viewer
  on the intermediate merge).
* **Data source config stays put:** the existing `dicomweb` entry pointed at
  Orthanc via nginx proxy is unchanged.
* **No new schema locks** — the response shape of our `/worklist` endpoint
  (documented in `integrations/worklist-api/main.py`'s module docstring)
  is what #21 consumes. If it needs to change, that is a joint change on
  both sides.
* **Priors/overlays: exact panel design is a #21 detail.** M2-plausible
  shape: a small right-panel that reads `?priorsRef=<studyContextRef>`
  from the URL and calls back to a companion endpoint (or reads a cached
  packet). Formalising the URL contract can happen at MR review time.
* **`StudyOpenedEvent` sink** — the current
  `contracts/events/ohif-opened.schema.json` requires
  `{schemaVersion, eventType, studyInstanceUID, openedAt}` and
  optionally `radiologistId`. Ingest surface for it is not yet wired in
  `orchestrator/ingress.py`; adding it is a small follow-up (~10 lines,
  parallel to the existing `orthanc_webhook`).

## Reference material

* [OHIF Modes / Routes](https://docs.ohif.org/platform/modes/routes/) —
  confirms that replacing the WorkList UI is done via a mode's
  `layoutTemplate.component`, not by writing a data source.
* [OHIF Data Source Module](https://docs.ohif.org/platform/extensions/modules/data-source/) —
  what Approach A would entail; scope reference for what we are
  deliberately *not* doing.
* [OHIF Customization Service — Custom Routes](https://docs.ohif.org/platform/services/customization-service/customroutes/) —
  lightweight alternative for pushing custom routes; considered but not
  chosen because a mode is a cleaner fit for the read-time hooks we need.
* [OHIF Study List behavior](https://v3-docs.ohif.org/user-guide/) —
  confirms the built-in Study List sorts by `StudyDate` desc under 100
  results and defers to server order otherwise; there is no config knob
  to inject external priority.
