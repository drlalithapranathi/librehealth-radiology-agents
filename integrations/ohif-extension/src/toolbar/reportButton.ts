/**
 * reportButton — OHIF toolbar button definition + registration helper for the
 * "Report this study" affordance (#73 criterion 1 follow-up per Saptarshi's !95).
 *
 * OHIF v3.6 toolbar contract (docs.ohif.org/platform/services/data/toolbarservice):
 *   * `toolbarService.addButtons([...defs])` registers button DEFINITIONS keyed by id.
 *   * `toolbarService.createButtonSection(section, [ids])` sets the ordered list of
 *     button ids rendered under a section. The default layout only renders the
 *     `primary` section, so a button must be in `primary` to actually appear.
 *
 * Ordering hazard: `createButtonSection('primary', [ids])` REPLACES the section's
 * contents (in 3.10+ it was renamed `updateSection` for exactly this reason). If our
 * extension's onModeEnter runs after the mode's onModeEnter, calling
 * `createButtonSection('primary', ['lhrad.report'])` would wipe MeasurementTools /
 * Zoom / WindowLevel / etc. and leave only our button. We avoid that by:
 *   1. reading the current primary section back if the service exposes it (defensive
 *      -- API not guaranteed across 3.6 minor releases);
 *   2. otherwise composing the merged list from the OHIF longitudinal mode's own
 *      canonical primary list, appending ours -- a known-good superset for the mode
 *      the dev stack runs.
 *
 * Alternative considered: register into a `secondary` section instead. Rejected
 * because OHIF's default `ViewerLayout` renders only `primary`; a secondary section
 * is invisible without a custom layout.
 */

/** The button id used both in the addButtons definition and the primary section list. */
export const REPORT_BUTTON_ID = 'lhrad.report';

/** The command id that gets invoked when the button is clicked (defined in the extension's
 *  getCommandsModule). */
export const REPORT_COMMAND_ID = 'lhrad.openReportForStudy';

/**
 * OHIF button definition for the "Report this study" action. `uiType: 'ohif.action'` is
 * the plain click-to-invoke shape (contrast with `ohif.toggle`, `ohif.splitButton`, etc.);
 * `commands: [{ commandName }]` is the standard command-invocation contract.
 */
export const reportButtonDefinition = {
  id: REPORT_BUTTON_ID,
  uiType: 'ohif.action',
  props: {
    id: REPORT_BUTTON_ID,
    // "external-link" (or similar) is one of the icons that ships with OHIF's default
    // icon set. If the deployed OHIF build lacks this icon, the button renders with the
    // fallback icon rather than failing to render at all.
    icon: 'external-link',
    label: 'Report',
    tooltip: 'Open the RIS order page to author this study\'s report',
    commands: [{ commandName: REPORT_COMMAND_ID }],
    // Always enabled: the underlying command handles the no-accession case by landing
    // on the RIS dashboard rather than a dead click. A per-URL enabled/disabled
    // evaluator would require a subscription to route changes, which is more moving
    // parts than the "always route somewhere useful" behavior is worth.
    evaluate: () => ({ disabled: false }),
  },
};

/**
 * OHIF's longitudinal mode primary section as of release/3.6. This is our safe
 * default when the running toolbarService doesn't expose the current section list;
 * we append our button to a known-good superset.
 *
 * If OHIF's default primary list drifts (a version bump adds a new tool button), the
 * worst case is that button is missing from the toolbar in our deployment until this
 * list catches up. It won't remove any of our functionality, and it won't crash.
 */
export const LONGITUDINAL_PRIMARY_DEFAULTS = [
  'MeasurementTools',
  'Zoom',
  'WindowLevel',
  'Pan',
  'Capture',
  'Layout',
  'Crosshairs',
  'MoreTools',
];

/**
 * Add the report button to the toolbar's primary section without stomping on whatever
 * buttons the mode's own onModeEnter registered. Idempotent per (mode enter, extension
 * onModeEnter) pair -- calling twice with the same toolbarService produces the same
 * result.
 *
 * Called from the extension's onModeEnter in src/index.ts. Kept here as a standalone
 * helper so it can be tested against a fake toolbarService without spinning up a mode.
 */
export function registerReportButtonOnPrimary(toolbarService: {
  addButtons: (defs: unknown[]) => void;
  createButtonSection: (name: string, ids: string[]) => void;
  // May or may not exist depending on the 3.6.x minor; guarded before we call it.
  getButtonSection?: (name: string) => string[] | undefined;
  getButtonPropsInSection?: (name: string) => Array<{ id: string }> | undefined;
}): void {
  // 1. Register the button definition. addButtons is idempotent per id.
  toolbarService.addButtons([reportButtonDefinition]);

  // 2. Compose the merged primary section list.
  let existing: string[] | undefined;
  try {
    // Some 3.6 minors expose a getter for the current section; others don't. Try both
    // common shapes before falling back to the canonical defaults.
    if (typeof toolbarService.getButtonSection === 'function') {
      existing = toolbarService.getButtonSection('primary');
    } else if (typeof toolbarService.getButtonPropsInSection === 'function') {
      const props = toolbarService.getButtonPropsInSection('primary');
      if (Array.isArray(props)) existing = props.map((p) => p.id);
    }
  } catch {
    // ignore — fall back to defaults
  }
  const base = existing && existing.length ? existing : LONGITUDINAL_PRIMARY_DEFAULTS;
  // Skip if already present (idempotency on double-enter, e.g. mode switches).
  const merged = base.includes(REPORT_BUTTON_ID) ? base : [...base, REPORT_BUTTON_ID];

  // 3. Update the section. `createButtonSection` in 3.6 is upsert semantics.
  toolbarService.createButtonSection('primary', merged);
}
