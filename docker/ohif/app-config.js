/**
 * OHIF viewer runtime config for the LH-Radiology dev stack.
 * The stock image ships a 0-byte app-config.js; this points OHIF at Orthanc's DICOMweb
 * through the same-origin nginx proxy (/dicom-web, /wado -> orthanc:8042), so there is
 * no cross-origin request and no Orthanc CORS to configure.
 */

// The ohif/app image bundles a WASM DICOM codec whose Emscripten glue is mis-compiled for the
// browser: it evaluates the Node globals __filename/__dirname unconditionally. When that chunk
// lazy-loads (codec worker init) it throws `ReferenceError: __filename is not defined` and the
// viewer goes blank. app-config.js runs before any lazy chunk, so defining these globals here
// (empty is fine — the loader already prefers document.currentScript.src for the real path)
// prevents the crash. Do NOT define `global` — that would flip other libs into their Node path.
window.__filename = window.__filename || '';
window.__dirname = window.__dirname || '/';

window.config = {
  routerBasename: '/',
  // Our @lhrad/extension-worklist is compiled into the OHIF bundle at build
  // time via pluginConfig.json (see integrations/ohif-extension/Dockerfile).
  // The /reading route is injected by the extension's preRegistration hook
  // via customizationService.setGlobalCustomization('customRoutes', ...) —
  // no custom mode is registered (see the R2 doc addendum). These arrays are
  // for RUNTIME dynamic registration of additional plugins loaded from URLs.
  extensions: [],
  modes: [],
  showStudyList: true,
  defaultDataSourceName: 'dicomweb',
  dataSources: [
    {
      friendlyName: 'Orthanc DICOMweb (proxied)',
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'dicomweb',
      configuration: {
        name: 'Orthanc',
        // Same-origin paths proxied by nginx to the Orthanc container.
        qidoRoot: '/dicom-web',
        wadoRoot: '/dicom-web',
        wadoUriRoot: '/wado',
        qidoSupportsIncludeField: true,
        supportsReject: true,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: true,
        supportsWildcard: true,
        omitQuotationForMultipartRequest: true,
      },
    },
  ],
};
