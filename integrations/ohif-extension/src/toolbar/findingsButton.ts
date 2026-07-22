/**
 * findingsButton — OHIF toolbar button that opens the AI Findings modal (#74,
 * client-side CAD evidence rendering; toolbar affordance per !95's finding that
 * panels don't mount in OHIF v3.6's default mode).
 *
 * Sibling to reportButton.ts. Shares the merge-safe primary-section append pattern
 * so both buttons live in the primary toolbar without stomping on the mode's own
 * buttons (MeasurementTools, Zoom, WindowLevel, etc.).
 *
 * Icon state doubles as ambient visibility of AI activity (the "banner at minimum"
 * criterion from the issue). The button's `evaluate` function returns different
 * className / disabled / tooltip props based on whether findings exist and their
 * severity:
 *   * COMPLETE finding present  -> colored icon (ambient signal there's evidence)
 *   * ERROR                     -> gray icon (AI ran but incomplete)
 *   * no findings / 404         -> muted icon (nothing to show yet)
 *
 * NOTE ON EVALUATE ASYNC-NESS: OHIF's evaluate() is called on every toolbar refresh
 * and expected to be synchronous or return quickly. A fetch-on-every-evaluate would
 * spam /reading-api/findings. We hold a cached last-known state per study UID in
 * module-local memory, refreshed lazily from evaluate() and by an explicit refresh
 * that showFindings can trigger after opening the modal.
 */

/** Button id used both in the addButtons definition and the primary section list. */
export const FINDINGS_BUTTON_ID = 'lhrad.findings';

/** Command id invoked when the button is clicked (defined in the extension's
 *  getCommandsModule). */
export const FINDINGS_COMMAND_ID = 'lhrad.showFindings';

// --------------------------------------------------------------------------------
// Icon-state cache — module-local, per-study last-known.
// --------------------------------------------------------------------------------
type IconTint = 'colored' | 'gray' | 'muted';

const _iconStateByStudy: Map<string, IconTint> = new Map();

/** External hook for showFindings.ts (or any code path that just fetched fresh data)
 *  to poke the icon into refreshed state without waiting for the next evaluate tick. */
export function setFindingsIconState(studyInstanceUID: string, tint: IconTint): void {
  _iconStateByStudy.set(studyInstanceUID, tint);
}

/** Reset the cache — used by tests to isolate module state between runs. */
export function _resetFindingsIconStateCacheForTests(): void {
  _iconStateByStudy.clear();
  _currentStudyUidOverride = undefined;
}

/** Test-only override so tests can drive currentStudyUid without depending on the
 *  DOM's location.search (happy-dom's history.pushState does not update the URL
 *  reliably; jsdom would, but the extension package uses happy-dom for speed).
 *  Undefined = fall through to the real window-reading path used at runtime. */
let _currentStudyUidOverride: string | null | undefined = undefined;

/** Test-only setter — mirrors what a route watcher would call at runtime. */
export function _setCurrentStudyUidForTests(uid: string | null): void {
  _currentStudyUidOverride = uid;
}

function currentStudyUid(): string | null {
  if (_currentStudyUidOverride !== undefined) return _currentStudyUidOverride;
  if (typeof window === 'undefined') return null;
  try {
    const raw = new URLSearchParams(window.location.search).get('StudyInstanceUIDs');
    if (!raw) return null;
    return raw.split(',')[0] || null;
  } catch {
    return null;
  }
}

function currentTint(): IconTint {
  const uid = currentStudyUid();
  if (!uid) return 'muted';
  return _iconStateByStudy.get(uid) ?? 'muted';
}


/**
 * OHIF button definition for the findings modal.
 * `uiType: 'ohif.action'` matches reportButton's shape — plain click-to-invoke.
 * `evaluate` returns className tokens so OHIF renders the button in the right tint
 * for the current study's finding state.
 */
export const findingsButtonDefinition = {
  id: FINDINGS_BUTTON_ID,
  uiType: 'ohif.action',
  props: {
    id: FINDINGS_BUTTON_ID,
    // 'sparkles' or 'info' — either ships with OHIF's default icon set. If a deployed
    // build lacks the exact name, the fallback icon renders rather than failing.
    icon: 'info',
    label: 'AI',
    tooltip: 'View the AI evidence banner for this study',
    commands: [{ commandName: FINDINGS_COMMAND_ID }],
    evaluate: () => {
      const tint = currentTint();
      // OHIF's evaluate contract: return an object with at least `disabled: boolean`.
      // We also expose the tint via className so the deployed OHIF theme can style
      // the different states (or a CSS override in app-config.js can pick it up).
      return {
        disabled: false,
        className: `lhrad-findings-button lhrad-findings-button--${tint}`,
      };
    },
  },
};

/**
 * Canonical longitudinal-mode primary defaults — shared safety net across both
 * reportButton and findingsButton registration. If OHIF's default primary drifts,
 * the worst case is a button is missing until this list catches up; never a crash.
 *
 * Kept in sync with LONGITUDINAL_PRIMARY_DEFAULTS in reportButton.ts. Duplicated
 * rather than imported to keep the two toolbar-button modules self-contained; if
 * they diverge, tests catch it.
 */
export const LONGITUDINAL_PRIMARY_DEFAULTS_FOR_FINDINGS = [
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
 * Add the findings button to the toolbar's primary section without wiping the mode's
 * own buttons. Same merge-safe strategy as registerReportButtonOnPrimary in
 * reportButton.ts — read the current section list, append if not present, fall back
 * to canonical longitudinal defaults if the toolbarService doesn't expose a getter.
 *
 * Idempotent per (mode enter, extension onModeEnter) pair.
 */
export function registerFindingsButtonOnPrimary(toolbarService: {
  addButtons: (defs: unknown[]) => void;
  createButtonSection: (name: string, ids: string[]) => void;
  getButtonSection?: (name: string) => string[] | undefined;
  getButtonPropsInSection?: (name: string) => Array<{ id: string }> | undefined;
}): void {
  toolbarService.addButtons([findingsButtonDefinition]);

  let existing: string[] | undefined;
  try {
    if (typeof toolbarService.getButtonSection === 'function') {
      existing = toolbarService.getButtonSection('primary');
    } else if (typeof toolbarService.getButtonPropsInSection === 'function') {
      const props = toolbarService.getButtonPropsInSection('primary');
      if (Array.isArray(props)) existing = props.map((p) => p.id);
    }
  } catch {
    // ignore — fall back to defaults
  }
  const base =
    existing && existing.length ? existing : LONGITUDINAL_PRIMARY_DEFAULTS_FOR_FINDINGS;
  const merged = base.includes(FINDINGS_BUTTON_ID) ? base : [...base, FINDINGS_BUTTON_ID];
  toolbarService.createButtonSection('primary', merged);
}
