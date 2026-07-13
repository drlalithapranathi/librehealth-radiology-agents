/**
 * Extension entry point — referenced by OHIF's `platform/app/pluginConfig.json`
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
 *   * preRegistration — injects a top-level `/reading` route into OHIF's
 *     router via customizationService, so the WorkList renders OUTSIDE
 *     DataSourceWrapper (i.e., without requiring StudyInstanceUIDs in the URL).
 *     See docs/ohif-integration-approach.md addendum for the design rationale.
 *   * getPanelModule — the PriorsPanel for the viewer route (opens once a
 *     radiologist clicks into a study and enters `/viewer/...`).
 *   * getLayoutTemplateModule — kept for future use if we want to substitute
 *     the WorkList into a mode's layout template.
 */
import * as React from 'react';

import { WorkList } from './components/WorkList';
import { PriorsPanel } from './components/PriorsPanel';

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
   * so it renders unconditionally — no StudyInstanceUIDs required. This is
   * why we do NOT use a custom mode for the worklist: OHIF modes are
   * study-viewer wrappers and always gate on DataSourceWrapper.
   */
  preRegistration({ servicesManager }: ExtensionContext) {
    const { customizationService } = servicesManager.services;

    // Fetch any existing customRoutes (defensive — another extension may have
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
        name: 'lhrad-priors',
        iconName: 'clipboard-list',
        iconLabel: 'Priors',
        label: 'Priors & Alerts',
        component: PriorsPanel,
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
};

export default extension;