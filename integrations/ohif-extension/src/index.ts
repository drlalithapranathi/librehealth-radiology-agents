/**
 * Extension entry point â€" referenced by OHIF's `platform/app/pluginConfig.json`
 * (see integrations/ohif-extension/Dockerfile which drops this into
 * `extensions/lhrad-extension-worklist/` of the Viewers workspace at build time).
 *
 * OHIF v3 extension contract (from https://docs.ohif.org/platform/extensions/):
 * The default export is an object with `id` plus zero-or-more of preRegistration,
 * getCommandsModule, getViewportModule, getLayoutTemplateModule, getPanelModule,
 * getToolbarModule, getSopClassHandlerModule, getHangingProtocolModule,
 * getUtilityModule.
 *
 * We provide:
 *   * preRegistration â€" injects a top-level `/reading` route into OHIF's
 *     router via customizationService, so the WorkList renders OUTSIDE
 *     DataSourceWrapper (i.e., without requiring StudyInstanceUIDs in the URL).
 *     See docs/ohif-integration-approach.md addendum for the design rationale.
 *   * getPanelModule â€" the PriorsPanel for the viewer route (opens once a
 *     radiologist clicks into a study and enters `/viewer/...`).
 *   * getLayoutTemplateModule â€" kept for future use if we want to substitute
 *     the WorkList into a mode's layout template.
 */
import * as React from 'react';

import { WorkList } from './components/WorkList';
import { ReportActionsPanel } from './components/ReportActionsPanel';
import { cxrTwoViewHangingProtocol } from './hangingProtocols/cxrTwoView';
// PriorsPanel intentionally not registered right now — the backing /priors-api/context/<ref>
// endpoint is not wired, so registering the panel would produce an idle-empty right column
// that reads as broken. Hidden here until priors resolution matures; the component itself
// stays in the tree for the follow-up. See #73 item 3 discussion + docs/ohif-integration-approach.md.

const EXTENSION_ID = '@lhrad/extension-worklist';

type ExtensionContext = {
  servicesManager?: any;
  commandsManager?: unknown;
  extensionManager?: unknown;
  configuration?: Record<string, unknown>;
};

const extension = {
  id: EXTENSION_ID,

  /**
   * preRegistration runs during extension registration, BEFORE createRoutes()
   * builds the router (see platform/app/src/routes/index.tsx line ~55, which
   * reads customizationService.getGlobalCustomization('customRoutes') and
   * spreads its routes into allRoutes). Setting the customization here means
   * OHIF's own router picks up `/reading` -> WorkList without any source patch.
   *
   * The route is registered OUTSIDE DataSourceWrapper (unlike mode routes),
   * so it renders unconditionally â€" no StudyInstanceUIDs required. This is
   * why we do NOT use a custom mode for the worklist: OHIF modes are
   * study-viewer wrappers and always gate on DataSourceWrapper.
   */
  preRegistration({ servicesManager }: ExtensionContext) {
    const { customizationService } = servicesManager.services;

    // Fetch any existing customRoutes (defensive â€" another extension may have
    // set one first) and merge our /reading route in.
    const existing =
      customizationService.getGlobalCustomization('customRoutes') || {};
    const existingRoutes = existing.routes || [];

    customizationService.setGlobalCustomization('customRoutes', {
      ...existing,
      routes: [
        ...existingRoutes,
        {
          path: '/reading',
          children: WorkList,
        },
      ],
    });
  },

  /**
   * Panel registered as `<extensionId>.panelModule.lhrad-priors`. Modes
   * reference this in their layoutTemplate props' rightPanels array to
   * surface it in the viewer at /viewer/... (post-worklist-click).
   */
  getPanelModule(_ctx: ExtensionContext) {
    return [
      {
        name: 'lhrad-report-actions',
        iconName: 'clipboard-list',
        iconLabel: 'Report',
        label: 'Report Actions',
        component: ReportActionsPanel,
      },
    ];
  },

  /**
   * Layout template kept for future mode use (e.g., if we later want a
   * study-viewer mode that substitutes the WorkList into its own layout).
   * Not consumed by the /reading route above.
   */
  getLayoutTemplateModule(_ctx: ExtensionContext) {
    return [
      {
        name: 'readingWorklist',
        id: 'readingWorklist',
        component: WorkList,
      },
    ];
  },

  /**
   * Hanging protocols (#73 item 4). CXR two-view: PA + LAT side-by-side.
   * Registered here so OHIF picks it up when it initializes the extension; the
   * matching rules inside the protocol scope it to CXR two-view studies.
   */
  getHangingProtocolModule(_ctx: ExtensionContext) {
    return [cxrTwoViewHangingProtocol];
  },
};

export default extension;