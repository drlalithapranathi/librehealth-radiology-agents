/**
 * @lhrad/mode-reading — the thin LibreHealth reading mode (#73/#74 affordances).
 *
 * WHY A MODE, after two toolbar/panel attempts died in review: OHIF v3.6's
 * default (longitudinal) mode owns its layout authoritatively. Extension
 * `getPanelModule` entries are never mounted by a mode that does not list
 * them, and extension `onModeEnter` toolbar-section appends are clobbered
 * when the mode's own `onModeEnter` recreates the primary section (both
 * live-verified against the built viewer, 2026-07-22, on !95 and !98). A
 * mode is the one place OHIF lets us declare panels deterministically — no
 * lifecycle races, no per-build button-shape guessing.
 *
 * THIN by construction: this wraps `@ohif/mode-longitudinal`'s factory and
 * changes exactly three things —
 *   1. identity: id `@lhrad/mode-reading`, route `/read` (the reading
 *      worklist's buildViewerUrl targets it; the stock `/viewer` route keeps
 *      working untouched for anything that still links to it);
 *   2. right panels: prepends the LH panels — FindingsBannerPanel first,
 *      ReportActionsPanel second — ahead of the stock Segmentation/
 *      Measurements panels, and opens the right panel by default so a
 *      positive study shows the AI finding with zero extra clicks (#74
 *      criterion 1 as written);
 *   3. extension dependency on `@lhrad/extension-worklist`, which provides
 *      those panels.
 * Everything else — toolbar, hotkeys, viewports, sop class handlers, hanging
 * protocol wiring — is the upstream mode's, by reference, so an OHIF bump
 * changes nothing here.
 */
import longitudinalMode from '@ohif/mode-longitudinal';

const EXTENSION_ID = '@lhrad/extension-worklist';
const MODE_ID = '@lhrad/mode-reading';

const FINDINGS_PANEL = `${EXTENSION_ID}.panelModule.lhrad-findings-banner`;
const REPORT_PANEL = `${EXTENSION_ID}.panelModule.lhrad-report-actions`;

const extensionDependencies = {
  ...longitudinalMode.extensionDependencies,
  [EXTENSION_ID]: '^0.1.0',
};

function modeFactory(...factoryArgs) {
  const mode = longitudinalMode.modeFactory(...factoryArgs);

  mode.id = MODE_ID;
  mode.routeName = 'read';
  mode.displayName = 'LH Reading';
  mode.extensions = { ...mode.extensions, [EXTENSION_ID]: '^0.1.0' };

  mode.routes = mode.routes.map(route => {
    const originalLayoutTemplate = route.layoutTemplate;
    return {
      ...route,
      layoutTemplate: (...ltArgs) => {
        const layout = originalLayoutTemplate(...ltArgs);
        layout.props.rightPanels = [
          FINDINGS_PANEL,
          REPORT_PANEL,
          ...(layout.props.rightPanels || []),
        ];
        // Open by default: the demo's "opening a positive study shows the AI
        // finding visually" criterion should not need a panel-tab click.
        layout.props.rightPanelDefaultClosed = false;
        return layout;
      },
    };
  });

  return mode;
}

const mode = {
  id: MODE_ID,
  modeFactory,
  extensionDependencies,
};

export default mode;
