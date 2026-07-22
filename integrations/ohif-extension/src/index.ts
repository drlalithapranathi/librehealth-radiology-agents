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
import { WorkList } from './components/WorkList';
import { ReportActionsPanel } from './components/ReportActionsPanel';
import { FindingsBannerPanel } from './components/FindingsBannerPanel';
import { cxrTwoViewHangingProtocol } from './hangingProtocols/cxrTwoView';
import { openReportForStudy } from './commands/openReportForStudy';
import { showFindings } from './commands/showFindings';
import {
  REPORT_BUTTON_ID,
  REPORT_COMMAND_ID,
  registerReportButtonOnPrimary,
} from './toolbar/reportButton';
import {
  FINDINGS_BUTTON_ID,
  FINDINGS_COMMAND_ID,
  registerFindingsButtonOnPrimary,
} from './toolbar/findingsButton';
// ReportActionsPanel and PriorsPanel are intentionally NOT registered as panels: OHIF
// v3.6's default mode does not mount extension panels (right panel bar renders only
// Segmentation/Measurements per Saptarshi's live browser drill on !95). ReportActionsPanel's
// functionality is now on the toolbar instead (see getCommandsModule + onModeEnter below).
// PriorsPanel stays unregistered pending its own backing endpoint. Both components remain
// in the source tree for tests + any future affordance route (thin lhrad mode, OHIF upgrade
// past the default-mode-mounts-panels limitation).

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
   * Panels are mounted by OUR mode, not the default one: OHIF v3.6's default
   * mode never mounts extension panels (verified in the !95 browser drill),
   * which is exactly why `@lhrad/mode-reading` exists (../mode/index.js). The
   * mode lists these two by their `<extensionId>.panelModule.<name>` ids in
   * its rightPanels, so the names here are load-bearing.
   */
  getPanelModule(_ctx: ExtensionContext) {
    return [
      {
        // icon name checked against the BUILT bundle's icon set ('tab-studies'
        // is absent in this OHIF pin and renders a "Missing Icon" tile)
        name: 'lhrad-findings-banner',
        iconName: 'tab-patient-info',
        iconLabel: 'AI',
        label: 'AI Findings',
        component: FindingsBannerPanel,
      },
      {
        name: 'lhrad-report-actions',
        iconName: 'tab-linear',
        iconLabel: 'Report',
        label: 'Report Actions',
        component: ReportActionsPanel,
      },
    ];
  },

  /**
   * The "Report this study" command (#73 criterion 1). Invoked by the toolbar button
   * defined in getToolbarModule below. All state comes from window.location.search
   * (the accession the WorkList's row click passes through), so the command needs no
   * OHIF service context beyond the servicesManager reference OHIF injects — the
   * openReportForStudy helper handles fetch + window.open on its own.
   *
   * definitions[commandId] is the OHIF v3 shape: { commandFn, storeContexts?,
   * options? }. We use commandFn only.
   */
  getCommandsModule({ servicesManager }: ExtensionContext) {
    return {
      definitions: {
        [REPORT_COMMAND_ID]: {
          commandFn: () => openReportForStudy(),
        },
        // #74: opens the AI Findings modal via OHIF UIModalService. servicesManager is
        // captured at extension registration so the command has access to
        // uiModalService (or UIModalService — the older PascalCase key on some 3.6
        // minors; showFindings probes both).
        [FINDINGS_COMMAND_ID]: {
          commandFn: () => showFindings({ servicesManager }),
        },
      },
    };
  },

  /**
   * Toolbar button definition (#73 criterion 1). Registered here so the OHIF
   * ExtensionManager knows the button exists; actually PLACING it in the primary
   * section happens in onModeEnter below (because primary is mode-owned, and merging
   * happens per-enter rather than at extension init).
   */
  getToolbarModule(_ctx: ExtensionContext) {
    return [
      {
        name: REPORT_BUTTON_ID,
        defaultComponent: null,
        // Full definitions live in the toolbar/*.ts modules and are passed to
        // toolbarService.addButtons from onModeEnter. Some OHIF versions ingest the
        // definition here directly; we defer to the onModeEnter pass to keep one
        // source of truth and predictable ordering.
      },
      {
        name: FINDINGS_BUTTON_ID,
        defaultComponent: null,
      },
    ];
  },

  /**
   * Extension-level onModeEnter. OHIF calls this whenever the app enters any mode
   * (including the default longitudinal), which is exactly what we want for a button
   * that should surface across every mode a radiologist reads in.
   *
   * Adds the report button definition to toolbarService, then merges its id into the
   * primary section without stomping on the mode's own buttons (MeasurementTools,
   * Zoom, WindowLevel, etc.). See src/toolbar/reportButton.ts for the merge-safe
   * append logic and its recovery from a toolbarService without a section getter.
   */
  onModeEnter({ servicesManager }: ExtensionContext) {
    try {
      const { toolbarService } = servicesManager.services;
      if (!toolbarService) return;
      // #73 criterion 1 — Report toolbar button.
      registerReportButtonOnPrimary(toolbarService);
      // #74 — AI Findings toolbar button (modal-based, per !95's finding that panels
      // don't mount in the default mode).
      registerFindingsButtonOnPrimary(toolbarService);
    } catch (err) {
      // Never crash a mode enter on a toolbar-registration failure — the study still
      // opens; the buttons just won't appear. Logs help debugging without failing the
      // radiologist's read.
      // eslint-disable-next-line no-console
      console.warn('lhrad: toolbar button registration failed on mode enter:', err);
    }
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
    // OHIF's ExtensionManager expects { name, protocol } entries; a bare
    // protocol object registers as undefined and is silently dropped.
    return [{ name: cxrTwoViewHangingProtocol.id, protocol: cxrTwoViewHangingProtocol }];
  },
};

export default extension;