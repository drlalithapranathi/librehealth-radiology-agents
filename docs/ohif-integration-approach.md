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
   Assistant's context packet (#13). Overlays reference Interpretation
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
  workspace. **Registration is build-time, not runtime.** OHIF v3 has no
  runtime plugin loader and the `ohif/app` image is a pre-compiled bundle, so
  listing the extension in `window.config.extensions`/`modes`
  (`docker/ohif/app-config.js`) is necessary but NOT sufficient: those
  namespaces resolve only if the extension is compiled into the bundle. We ship
  it by building our own OHIF image (a multi-stage Dockerfile in
  `integrations/ohif-extension/`: a node stage that compiles OHIF v3.6.5 + our
  extension, then an nginx stage that serves the built `dist/`) and pointing
  docker-compose at that image instead of the stock `ohif/app`. This is a
  custom image build, NOT a fork of OHIF: we depend on OHIF and do not edit its
  source, so a version bump is a dependency bump and a rebuild. Note the Orthanc
  runtime-mount plugin pattern does not transfer here, because Orthanc has a
  plugin loader and OHIF does not.
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

---

## Addendum (issue #21 kickoff): the "just edit app-config.js" reading was wrong

Owner: Parvati. Status: **execution correction, does not change the decision.**
Prompted by [group chat with Saptarshi during #21 kickoff].

The original doc above said (in "Consequences for #21"): *"Registration in
`docker/ohif/app-config.js` deferred to the #21 MR itself so the extension
is only registered once it exists (avoids a broken viewer on the intermediate
merge)."* That sentence is materially wrong — it implied our extension could
be registered with an edit to `app-config.js` at runtime. OHIF v3 does not
work that way. `window.config.extensions = [...]` references modules that
must be **baked into the viewer bundle at build time**. Overriding
`app-config.js` handles runtime configuration (URLs, sort order, feature
flags); it cannot add new JavaScript modules to the running viewer.

The correct wording — folded into the #21 MR: registration requires our
**own OHIF image build**, not just an `app-config.js` edit. Concretely: a
Dockerfile in `integrations/ohif-extension/` runs a multi-stage build that
consumes OHIF at a pinned version (v3.6.5 today) plus our extension/mode
packages, produces a viewer bundle with everything compiled in, and nginx
serves it. `docker-compose.yml` switches `image:` → `build:` on the ohif
service. This is Saptarshi's clarification and it is what #21 ships.

### Why "thin-app" — build a small package consuming `@ohif/*` from npm — is not a viable middle path

Saptarshi's message suggested trying a thin-app spike *first*, with a
monorepo clone as the fallback. During #21 kickoff I checked; the thin-app
path does not exist as a real option. Documenting the reasoning so the
question doesn't get relitigated.

**A thin-app would need three things to be true simultaneously.**

1. **A consumable OHIF app-shell package on npm** — something like
   `@ohif/app` you can `npm install`, containing the webpack config, entry
   HTML, runtime bootstrap, extension-registration mechanism, and
   Cornerstone3D/WASM asset handling.
2. **A version reachable on npm** matching our Docker target.
3. **A compatible peer-dep tree** — React version, TypeScript version, and
   webpack version aligned.

**What I actually found in npm:**

`@ohif/core`, `@ohif/ui`, `@ohif/extension-default`, `@ohif/extension-cornerstone`
and friends are published as **libraries** — building blocks other projects
can consume. But `@ohif/app` is not on npm at all. `platform/app` inside
the Viewers monorepo is the *application itself* (webpack config, HTML
entry, bootstrap, service worker, i18n setup, extension registration
plumbing). It is not packaged for external consumption because it is not
a library.

To make thin-app work, we would have to **reimplement OHIF's application
shell** from scratch: our own webpack config that handles Cornerstone3D's
WASM assets, dynamic imports, CSS, and i18n; our own HTML entry; our own
bootstrap that mirrors what `platform/app` does when it registers
extensions and mounts the router. That is not "consuming a library." It
is a fork of the application layer, worse than the monorepo-clone path
because it detaches us from OHIF's upstream evolution while giving nothing
back.

Requirements 2 and 3 also had problems in isolation (npm skips patch
versions, so `3.6.5` isn't published — the nearest is `3.6.0`; and the
current `@ohif/*` tree above 3.11 requires React 18 while v3.6.x is
React 17). But those are the sort of small mismatches you'd normally
work around. The blocker is requirement 1: **there is no OHIF app
package to consume.**

**Conclusion recorded here so a future contributor doesn't re-run the
spike:** the monorepo-clone path in the Dockerfile is not a fallback
after thin-app fails; it is the only reasonable route. Approach B (custom
mode + WorkList component + panel) still stands — this addendum only
corrects HOW it gets built and shipped, not what gets built.

### Bonus workarounds we can now drop

Owning the image build lets us drop two workarounds carried today for the
stock `ohif/app:v3.6.5` image:

* The `__filename`/`__dirname` WASM shim at the top of
  `docker/ohif/app-config.js` — needed today because a codec worker chunk
  in the stock image was Emscripten-compiled with Node globals referenced
  unconditionally. Once we control the build, we can patch the codec at
  compile time (or upgrade past the buggy version) and drop the shim.
* The `gzip_static on;` dance in `docker/ohif/default.conf` — needed today
  because the stock image ships JS/CSS as 0-byte placeholders with content
  in matching `.gz` files. Our build can emit uncompressed bundles (nginx
  can gzip on the fly) and drop the pre-compression step.

**Not in the #21 MR.** Both workarounds stay in this MR to keep scope
tight and let us verify the custom image is otherwise identical to the
stock one before subtracting anything. Small follow-up MR after #21
lands.

### Consequences for #21 — corrected

- Skeleton lives at `integrations/ohif-extension/` (this MR fills it).
- **Extension packaged as npm workspace packages inside the OHIF Viewers
  monorepo at build time** via `integrations/ohif-extension/Dockerfile`.
  Our source tree is one package for local dev ergonomics; the Dockerfile
  splits it into `extensions/lhrad-extension-worklist/` and
  `modes/lhrad-mode-reading/` inside the workspace so OHIF's
  `pluginConfig.json` can list them as distinct packages per convention.
- `docker/ohif/app-config.js` gets updated in this MR to register
  `@lhrad/extension-worklist` in `extensions[]` and `@lhrad/mode-reading`
  in `modes[]`.
- `docker-compose.yml` swaps `image: ohif/app@sha256:...` → `build:` on
  the `ohif` service in this MR.
- `docker/ohif/default.conf` adds `/reading-api/*` and
  `/orchestrator-api/*` reverse proxies so browser calls stay same-origin.
- `/worklist` response shape is stable as documented in
  `integrations/worklist-api/main.py`; #21 consumes it via `WorklistItem`
  TS types in `integrations/ohif-extension/src/types.ts`.
- `contracts/events/ohif-opened.schema.json` shape verified against
  the emitter; ingest surface (`POST /orchestrator-api/events/ohif-opened`
  → `orchestrator/ingress.py`) is a small follow-up (~10 lines parallel
  to `orthanc_webhook`).
- Workaround cleanup deferred to a follow-up MR after #21 lands.


---

## Second addendum (issue #21 execution): pivot from custom mode to `customizationService.customRoutes`

Owner: Parvati. Status: **execution correction, does not change the R2
decision.** Prompted by empirical testing during #21 build-and-run.

### What we tried and why it didn't work

The first-addendum plan shipped Approach B as "custom mode + custom
WorkList component" packaged as two workspace packages
(`extensions/lhrad-extension-worklist/` and `modes/lhrad-mode-reading/`),
both compiled into the OHIF bundle via `pluginConfig.json`. The mode
registered a route at `/reading` whose `layoutTemplate` returned our
WorkList component. On paper this matched the OHIF Modes documentation.

On a live build, `/reading` rendered a blank dark page — no console
errors, no `onModeEnter` log firing, but React had mounted and OHIF's
services had all registered. Cross-testing against OHIF's own built-in
`/viewer` route (with no `?StudyInstanceUIDs=...` param) reproduced the
same blank dark page. The empirical finding: **OHIF v3 modes are
study-viewer wrappers by contract, not general-purpose routes.** Every
mode route is nested under OHIF's `DataSourceWrapper` (see
`platform/app/src/routes/index.tsx` and
`platform/app/src/routes/Mode/Mode.tsx`), which gates rendering on
study UIDs being present in the URL and silently renders null otherwise.
A worklist screen has no study UIDs by definition, so it renders
nothing.

The OHIF Modes docs describe the mode API as "OHIF-v3 shines… simply add
a new layoutTemplate," but the `DataSourceWrapper` gate isn't called out
there. It only surfaces when you actually load a mode without study UIDs
and see nothing. A future OHIF release may relax the gate (there is an
open feature request along these lines) but v3.6.5 does not.

### What we did instead

The R2 doc's own Reference-material section had already flagged the
alternative: OHIF's [Customization Service — Custom Routes](https://docs.ohif.org/platform/services/customization-service/customroutes/).
At R2 time we'd noted it as "considered but not chosen because a mode is
a cleaner fit for the read-time hooks we need"; the empirical evidence
above inverts that ranking.

`platform/app/src/routes/index.tsx` (line ~55) reads
`customizationService.getGlobalCustomization('customRoutes')` at
router-build time and spreads `customRoutes.routes` into `allRoutes`
**outside** the `DataSourceWrapper` gate. That's the extension point
we now use:

- The extension's `preRegistration` hook calls
  `customizationService.setGlobalCustomization('customRoutes', { routes: [{ path: '/reading', children: WorkList }] })`.
- OHIF's router wires `/reading` → WorkList directly. No mode, no
  DataSourceWrapper, no study-UID gate.
- `StudyOpenedEvent` still fires on row click inside the WorkList
  (unchanged from the original design).
- The read-time hooks the R2 note mentioned as reasons to prefer a mode
  (`onModeEnter` / `onModeExit`) are not needed on `/reading` itself,
  because `/reading` is the *worklist* screen. Those hooks fire when a
  radiologist opens a *study*, which happens on OHIF's built-in
  `/viewer/...` route — which we're not overriding. The mode-vs-route
  tradeoff at R2 time misidentified where those hooks would fire.

### Consequences for #21 — corrected again

Changes from the first-addendum's "Consequences for #21 — corrected"
list:

- **One workspace package, not two.** The extension packages as
  `extensions/lhrad-extension-worklist/` only. No
  `modes/lhrad-mode-reading/`, no mode source file, no mode-package
  synthesis in the Dockerfile.
- **`pluginConfig.json` registers extensions only.** The Dockerfile's
  registration step no longer pushes into `cfg.modes`.
- **`docker/ohif/app-config.js` keeps `extensions: []` and `modes: []`
  both empty**, and the comment there is updated to reflect that
  `/reading` is a `customizationService.customRoutes` injection rather
  than a compiled-in mode.
- **`docker-compose.yml` swap** (`image:` → `build:` on the ohif
  service) is unchanged.
- **`docker/ohif/default.conf`** reverse-proxy additions are unchanged
  (`/reading-api/*` → worklist-api; `/orchestrator-api/*` still
  commented out pending the ingress endpoint).
- **`contracts/events/ohif-opened.schema.json`** unchanged; ingest
  surface still a small follow-up.
- **Bonus workarounds section** unchanged and still deferred to a
  follow-up MR.

### Why this is strictly better than the mode path we originally chose

- No OHIF source is patched. The R2 doc's "compose OHIF at a pinned
  tag" framing is now accurate again, which it wouldn't have been if we
  had reached for `sed`-editing the router (the escape hatch we briefly
  considered before rediscovering `customRoutes`).
- Uses an OHIF-documented public API surface, so version-bump risk is
  bounded to `customizationService.setGlobalCustomization` staying
  stable — cheaper than tracking mode-contract or router shape changes.
- The extension package is smaller by one whole workspace, which is
  fewer moving pieces to reason about at review time.

Approach B still stands as the R2 decision; this addendum only corrects
HOW the WorkList reaches the URL, not what gets built.
## Third addendum (merge review): the v3.6.5 pin was not a pin

Found while verifying the MR before merge: OHIF/Viewers has NO `v3.6.5` git
tag. The v-prefixed tags stop at `v3.6.0` and the `@ohif/viewer@*` tags stop
at 3.6.3; the `ohif/app:v3.6.5` Docker Hub tag has no git twin. So the
Dockerfile's `git clone --branch v3.6.5 || git clone --branch release/3.6`
took the fallback on every single build and quietly built whatever
`release/3.6` pointed at that day. The old compose setup pinned the stock
image by digest for exactly this reason, so this was a step backwards in
reproducibility, and the `||` fallback would also have masked any future
typo in `OHIF_REF` forever.

Fixed at merge time:

- `OHIF_REF` now pins the exact commit the stack was validated against
  (`72ec0bf`, the `release/3.6` HEAD at review time), fetched directly via
  `git fetch --depth 1 origin "$OHIF_REF"`.
- A bad ref now FAILS the build instead of silently falling back.
- Version bumps stay a one line change: point `OHIF_REF` at a new commit or
  any fetchable ref (tag or branch name both work).

  
## Post-M2 wiring (#73)

Four click-path gaps closed. Each is inert or fallback-safe by default so the
change is safe to merge before the demo host is fully turned on.

### Item 1 — study-opened event sink

The extension has been POSTing `ohif.study.opened` to
`/orchestrator-api/events/ohif-opened` since M2 (see `eventClient.ts:22`), but
until this MR neither the nginx proxy block nor the receiving endpoint
existed. Both are wired now:

- `docker/ohif/default.conf` uncommented the `/orchestrator-api/` block so the
  extension's fetch reaches `http://ingress:8090/` on the compose network.
- `orchestrator/ingress.py` gained `POST /events/ohif-opened` — schema-validates
  against `contracts/events/ohif-opened.schema.json`, logs at INFO on the
  `orchestrator.ingress.ohif` logger, returns 202. The acceptance criterion
  is "visible in logs/store"; a first cut logs and defers store persistence
  until a downstream consumer needs durable events.

Best-effort by design end-to-end: producer returns `false` on any non-2xx and
never blocks navigation; consumer accepts anything schema-valid and does
nothing safety-critical with it.

### Item 2 — viewer → RIS handoff

A new right-side panel `ReportActionsPanel` renders a **"Report this study"**
button that opens the RIS report authoring page in a new tab, accession-
parameterized. The URL is built from a template constant that can be overridden
per-deployment via a global on `window.LHRAD_RIS_REPORT_URL_TEMPLATE`
(configured in the OHIF `app-config.js`) so a deployment can point at whichever
UI its OpenMRS serves without a code deploy.

Accession propagation: the WorkList row click passes both StudyInstanceUID
and AccessionNumber to `buildViewerUrl`, which now emits
`?StudyInstanceUIDs=<uid>&accession=<acc>`. The panel reads `?accession=`
from `window.location.search` and substitutes it into the template.

**Default URL template pending confirmation.** The default in the code is
`/openmrs/owa/radiologyapp/index.html#/studies?accession={accession}` which
follows the LibreHealth Radiology OWA convention documented at
[forums.librehealth.io](https://forums.librehealth.io/t/project-implementing-reporting-workflow-for-radiology-as-an-open-web-app-and-integrating-voice-dictation-for-radiology/2343);
however, the current dev-stack o3 image may serve reporting via a different
route. A deployment overrides via the global; a code change swaps the default.
See MR description for coordination.

### Item 3 — PriorsPanel

`PriorsPanel` is **unregistered from `getPanelModule`** in this MR. The
component itself stays in the source tree; only the panel-module registration
is removed. Rationale:

- The panel fetches `/priors-api/context/<ref>` which has no proxy and no
  backing service, so registering it produces an idle-empty right column that
  reads as broken.
- Backing it means adding a `/priors-api/context/<ref>` endpoint on
  `worklist-api` (natural host, per the issue) that resolves a study reference
  to a `PriorsPacket`. `fhir_client.list_prior_studies` and `list_active_problems`
  already return the right shape, but the resolver-and-route work is out of
  scope for closing #73 — the acceptance criterion is "either shows the current
  study's priors or is absent; never idle-empty," and absent is the honest
  answer today.
- Follow-up: file a separate issue "Back PriorsPanel with a small resolver on
  worklist-api" once priors data quality is confirmed for the demo cohort.
  Bringing the panel back is a two-line change: re-add the import and the
  panel-module entry.

### Item 4 — CXR hanging

New `cxrTwoViewHangingProtocol` in
`integrations/ohif-extension/src/hangingProtocols/cxrTwoView.ts`. Matches CXR
studies (Modality in `{CR, DX, CX}`) with 2+ series and lays PA and LAT
side-by-side in a 2-column grid. Series assignment prefers `ViewPosition` tags
and falls back to positional (first series → left, second → right) so the
display is at least side-by-side even if the specific-side call is uncertain.

Registered via a new `getHangingProtocolModule` on the extension. A
single-view CXR (only PA) falls through to OHIF's default 1-up, which is
correct for one view.
