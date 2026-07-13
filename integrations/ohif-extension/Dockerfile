# syntax=docker/dockerfile:1.6
#
# LH-Radiology OHIF build.
#
# ARCHITECTURE (per docs/ohif-integration-approach.md addendum):
# There is no viable "thin-app" path -- OHIF does NOT publish a consumable
# app-shell npm package (@ohif/app doesn't exist; platform/app is application
# source living only inside the Viewers monorepo). So this Dockerfile clones
# the OHIF Viewers repo at the pinned release, drops our single extension
# package into the workspace, registers it in pluginConfig.json, and runs
# OHIF's own build. We are COMPOSING OHIF plus our plugin, not maintaining a
# fork (per Saptarshi's #21 review): a version bump is a dependency-tag bump
# on line ~30 below, nothing to rebase.
#
# HOW /reading GETS ROUTED (design note, see R2 doc addendum):
# We do NOT ship a custom OHIF "mode" for the reading worklist. OHIF modes
# are study-viewer wrappers: every mode route is nested under DataSourceWrapper
# and gated on `?StudyInstanceUIDs=...`, so a worklist-screen mode with no
# study renders blank. Instead the extension's preRegistration hook injects
# `/reading -> WorkList` via customizationService's `customRoutes` global
# customization (see platform/app/src/routes/index.tsx line ~55, which
# spreads customRoutes.routes into allRoutes OUTSIDE DataSourceWrapper).
# That's the intended extension point for app-level routes; no OHIF source
# is patched here.
#
# Stage 1 (builder) is heavy: it clones ~600 MB, installs Cornerstone3D's
# native deps, and runs the OHIF webpack build. First `docker build` will
# take 10-20 minutes; subsequent rebuilds hit the Yarn cache and are ~3-5
# minutes if only our extension source changed.
#
# Stage 2 (nginx) serves the built dist/. Same nginx config we use today for
# the stock ohif/app image (SPA index fallback, gzip_static, /dicom-web
# proxy to Orthanc). One workaround from the stock image can now be dropped:
# the __filename WASM shim in app-config.js. Left in for this MR to avoid
# scope creep; noted as a follow-up in the R2 doc addendum.

# ---------- Stage 1: build the OHIF viewer with our extension ---------------

FROM node:20-bookworm-slim AS builder

# Native deps Cornerstone3D pulls in during install (via node-gyp). Trimmed
# from the OHIF Viewers Dockerfile -- see their platform/app/Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# Pin the OHIF release. OHIF publishes the docker image as `ohif/app:v3.6.5`
# which corresponds to the `v3.6.5` git tag in OHIF/Viewers. When we bump this
# to (say) v3.7.x, this is the ONE line to change -- no lockfile to rebase, no
# extension code to touch. If the tag turns out not to exist under that exact
# name (OHIF has flipped between `v3.6.5` and `3.6.5`), the fallback branch
# `release/3.6` is a compatible near-neighbor.
ARG OHIF_REF=v3.6.5
RUN git clone --depth 1 --branch "${OHIF_REF}" \
      https://github.com/OHIF/Viewers.git . \
    || git clone --depth 1 --branch "release/3.6" \
      https://github.com/OHIF/Viewers.git .

# Drop our extension into the workspace. Copying into extensions/ means Yarn
# workspaces picks it up automatically as `@lhrad/extension-worklist`. This
# is a single-package drop-in: no separate modes/ package, because /reading
# is registered via customizationService (see architecture note above), not
# via OHIF's mode registry.
COPY integrations/ohif-extension/package.json      /tmp/lhrad-src/
COPY integrations/ohif-extension/tsconfig.json     /tmp/lhrad-src/
COPY integrations/ohif-extension/src               /tmp/lhrad-src/src

# Extension package -- everything except test files. tsconfig comes along so
# OHIF's TypeScript build picks up our compiler options.
RUN mkdir -p extensions/lhrad-extension-worklist/src \
    && cp -r /tmp/lhrad-src/src/api extensions/lhrad-extension-worklist/src/ \
    && cp -r /tmp/lhrad-src/src/components extensions/lhrad-extension-worklist/src/ \
    && cp /tmp/lhrad-src/src/types.ts extensions/lhrad-extension-worklist/src/ \
    && cp /tmp/lhrad-src/src/index.ts extensions/lhrad-extension-worklist/src/ \
    && cp /tmp/lhrad-src/tsconfig.json extensions/lhrad-extension-worklist/

# package.json for the workspace extension. Generated here rather than
# committed to the repo, because our local single-package layout doesn't need
# it -- it exists only inside the OHIF workspace.
RUN cat > extensions/lhrad-extension-worklist/package.json <<'JSON'
{
  "name": "@lhrad/extension-worklist",
  "version": "0.1.0",
  "private": true,
  "main": "src/index.ts",
  "peerDependencies": {
    "@ohif/core": "*",
    "@ohif/ui": "*",
    "react": "17.x",
    "react-dom": "17.x"
  }
}
JSON

# Register our extension in pluginConfig.json. We rewrite the file with node
# rather than sed so we survive any whitespace variations in the upstream OHIF
# file. If OHIF changes the plugin config shape in a future release, this is
# where debugging starts.
#
# NOTE: we do NOT push anything into cfg.modes -- the reading worklist is a
# customizationService route, not a mode (see architecture note above).
RUN node -e ' \
  const fs = require("fs"); \
  const path = "platform/app/pluginConfig.json"; \
  const cfg = JSON.parse(fs.readFileSync(path, "utf8")); \
  cfg.extensions = cfg.extensions || []; \
  if (!cfg.extensions.some(e => (e.packageName||e) === "@lhrad/extension-worklist")) { \
    cfg.extensions.push({ packageName: "@lhrad/extension-worklist", version: "0.1.0" }); \
  } \
  fs.writeFileSync(path, JSON.stringify(cfg, null, 2)); \
  console.log("registered extensions:", cfg.extensions.map(e => e.packageName||e)); \
'

# Install workspace deps. --frozen-lockfile is the OHIF-documented flag for
# reproducible builds against their pinned lockfile. Our new workspace package
# is private and its deps resolve internally -- the lockfile doesn't need to
# regenerate.
RUN yarn config set workspaces-experimental true \
    && yarn install --frozen-lockfile --network-timeout 600000

# Build the OHIF app. `yarn build` in the monorepo root runs the platform/app
# webpack production build; output lands in platform/app/dist. Our extension
# gets compiled in via pluginConfig.json.
ENV PUBLIC_URL=/
ENV APP_CONFIG=config/default.js
RUN yarn build

# ---------- Stage 2: serve the built dist ------------------------------------

FROM nginx:1.27-alpine

# Same nginx config we use today with the stock image -- SPA index fallback,
# gzip_static (the OHIF build ships pre-compressed assets), and the /dicom-web
# and /wado proxies to Orthanc. Kept as a bind mount in docker-compose so we
# can edit routing without rebuilding the image.
COPY --from=builder /work/platform/app/dist /usr/share/nginx/html
# app-config.js is bind-mounted at runtime by docker-compose. Leave the built
# copy as a fallback so a docker run without the bind mount is still usable.

EXPOSE 80