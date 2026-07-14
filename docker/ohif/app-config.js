/**
 * OHIF viewer runtime config for the LH-Radiology dev stack.
 * Points OHIF at Orthanc's DICOMweb through the same-origin nginx proxy
 * (/dicom-web, /wado -> orthanc:8042), so there is no cross-origin request
 * and no Orthanc CORS to configure.
 *
 * Since #21 we build the OHIF image ourselves (integrations/ohif-extension/
 * Dockerfile) rather than using the stock ohif/app image. The
 * __filename/__dirname WASM shim that used to live at the top of this file
 * was dropped in the #21 follow-up MR: every WASM codec OHIF's platform/app
 * pulls in at the pinned commit (@cornerstonejs/codec-charls,
 * codec-libjpeg-turbo-8bit, codec-openjpeg, codec-openjph, dicom-image-loader)
 * guards its __filename reference with `if (typeof __filename !== 'undefined')`
 * before evaluating it, so the ReferenceError the shim was preventing cannot
 * occur in our build. If a codec is ever upgraded to a version that reintroduces
 * a bare __filename reference, the shim is easy to reinstate here.
 */

window.config = {
  routerBasename: '/',
  // Our @lhrad/extension-worklist is compiled into the OHIF bundle at build
  // time via pluginConfig.json (see integrations/ohif-extension/Dockerfile).
  // The /reading route is injected by the extension's preRegistration hook
  // via customizationService.setGlobalCustomization('customRoutes', ...) --
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
