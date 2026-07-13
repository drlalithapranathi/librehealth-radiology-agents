# LH-Radiology OHIF Extension (issue #21)

Custom OHIF v3 extension + mode that:

1. Serves a **priority-ordered reading worklist** at `/reading`, sourced from
   the Worklist API (`/reading-api/worklist`, issue #20).
2. Emits `ohif.study.opened` (see `contracts/events/ohif-opened.schema.json`)
   when the radiologist opens a study.
3. Surfaces **priors and contrast alerts** in a right-side viewer panel,
   sourced from the EHR Assistant context packet (issue #4).

Owner: Parvati. Approach + build decision: see
[`docs/ohif-integration-approach.md`](../../docs/ohif-integration-approach.md).

## How this ships

The extension is packaged into OHIF at Docker build time via a multi-stage
`Dockerfile` in this directory that clones OHIF Viewers at v3.6.5, drops our
extension + mode packages into the workspace, registers them in
`platform/app/pluginConfig.json`, and runs OHIF's own webpack build. See the
R2 doc addendum for why the thin-app path (consuming `@ohif/*` from npm) is
not viable.

`docker-compose.yml`'s `ohif:` service does `build:` on this Dockerfile. Bring
it up with the rest of the stack:

```bash
docker compose up -d --build ohif orthanc worklist-api
```

First build is slow (10–20 min: clones ~600 MB, installs Cornerstone3D native
deps, runs OHIF webpack production build). Subsequent rebuilds when only the
extension source changes are ~3–5 min.

Open http://localhost:3000/#/reading for the priority-ordered worklist. The
built-in OHIF Study List at http://localhost:3000/ still works (DICOMweb
against Orthanc, sorted by StudyDate desc).

## Local development

The extension source is a single npm package for dev ergonomics. Tests run
without OHIF installed — they cover the pieces that don't need OHIF's runtime
(the Worklist API client, the StudyOpenedEvent emitter, sorting logic, and the
WorkList component via happy-dom + @testing-library/react).

```bash
cd integrations/ohif-extension
npm install
npm test         # 30 tests, ~3 s
npm run typecheck
```

The pieces that DO need real OHIF (extension registration, mode routing,
layout template mounting, panel rendering) are covered by the Docker smoke
test — they only run once the extension is compiled into the viewer bundle.

## Source layout

```
src/
├── index.ts               # Extension entry: getLayoutTemplateModule + getPanelModule
├── mode.ts                # Custom mode registering /reading route
├── types.ts               # TS contracts mirroring /worklist response
├── api/
│   ├── worklistClient.ts # fetch + sort + WorklistApiError
│   └── eventClient.ts    # StudyOpenedEvent POST + viewer URL builder
└── components/
    ├── WorkList.tsx      # Priority-ordered reading list
    └── PriorsPanel.tsx   # Priors + contrast alerts panel
```

## Assumptions verified during Docker build (heads-up for reviewers)

Several structural bits of the OHIF v3 extension API are my best read of the
docs, not something I could run against real OHIF in isolation. Flagged with
`DESIGN NOTE (verify during Docker build):` comments in:

- `src/index.ts` — exact `getPanelModule` / `getLayoutTemplateModule` return shape
- `src/mode.ts` — exact `layoutTemplate` return shape and mode config keys

If the Docker build reveals these need different structures, the change is
contained to those two files — the React components in `components/` and the
API clients in `api/` don't care how they get mounted.
