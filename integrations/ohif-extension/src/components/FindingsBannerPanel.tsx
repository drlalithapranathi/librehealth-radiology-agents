/**
 * FindingsBannerPanel — renders AI evidence for the currently-open study (#74,
 * client-side CAD evidence). Displayed inside OHIF's UIModalService modal that the
 * `lhrad.showFindings` toolbar button opens; #73's browser drill on !95 proved OHIF
 * v3.6's default mode does NOT mount extension panels, so a right-side panel
 * registration would be invisible. Toolbar-button + modal is the affordance route
 * per Saptarshi's option (a).
 *
 * Component itself is affordance-agnostic — it reads studyInstanceUID from the URL
 * (or props for tests), fetches findings, applies rendering policy locally, renders
 * as a self-contained React tree. If a future affordance route lands (thin lhrad
 * mode with a persistent panel, OHIF upgrade past the default-mode-panels
 * limitation), this component drops in unchanged.
 *
 * Rendering policy (unchanged from the earlier panel-based draft):
 *
 *   * COMPLETE  -> prominent banner: tool + label + confidence
 *   * ERROR     -> subdued neutral state: "AI scan incomplete for this study"
 *   * STUBBED   -> nothing (no false marks; STUBBED means "tool ran but no evidence"
 *                  or "tool couldn't look" — never a positive claim)
 *   * PARTIAL   -> render each finding by its own status
 *
 * Fetch returns null (404 or error) -> subtle "AI analyzing or unavailable" hint.
 * Distinguishes "not yet published" (404) from "published empty" (200 with all
 * STUBBED findings): both look similar to the reader but they're semantically
 * distinct and could grow different UX later.
 *
 * No PHI rendered. Findings carry tool label + numeric confidence; evidenceRef
 * points at an Orthanc instance id but is not rendered as text.
 */
import * as React from 'react';

import { fetchFindings, FindingItem, FindingsResponse } from '../api/findingsClient';

const STUDY_URL_PARAM = 'StudyInstanceUIDs';

export interface FindingsBannerPanelProps {
  /** Overridable for tests. Reads window.location.search by default. */
  studyInstanceUID?: string | null;
  /** Overridable for tests. */
  fetchImpl?: typeof fetchFindings;
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'not-yet' }       // fetch returned null (404 or error)
  | { kind: 'ready'; data: FindingsResponse };

export const FindingsBannerPanel: React.FC<FindingsBannerPanelProps> = ({
  studyInstanceUID,
  fetchImpl = fetchFindings,
}) => {
  const uid =
    studyInstanceUID !== undefined ? studyInstanceUID : readStudyUidFromLocation();

  const [state, setState] = React.useState<LoadState>({ kind: 'loading' });

  React.useEffect(() => {
    if (!uid) {
      setState({ kind: 'not-yet' });
      return;
    }
    const controller = new AbortController();
    setState({ kind: 'loading' });
    fetchImpl(uid, controller.signal).then((data) => {
      if (controller.signal.aborted) return;
      if (data === null) {
        setState({ kind: 'not-yet' });
      } else {
        setState({ kind: 'ready', data });
      }
    });
    return () => controller.abort();
  }, [uid, fetchImpl]);

  return (
    <div data-testid="lhrad-findings-banner-panel" style={styles.panel}>
      {renderBody(state)}
    </div>
  );
};

function renderBody(state: LoadState): React.ReactNode {
  if (state.kind === 'loading') {
    return (
      <p data-testid="lhrad-findings-loading" style={styles.hint}>
        Loading…
      </p>
    );
  }
  if (state.kind === 'not-yet') {
    return (
      <p data-testid="lhrad-findings-not-yet" style={styles.hint}>
        AI analysis in progress or unavailable for this study.
      </p>
    );
  }
  const findings = state.data.findings;
  const complete = findings.filter((f) => f.status === 'COMPLETE');
  const errored = findings.filter((f) => f.status === 'ERROR');
  // STUBBED deliberately not surfaced — "tool ran but no evidence" / "tool couldn't
  // look" is not a positive claim and rendering it would risk automation-bias false
  // reassurance.
  return (
    <>
      {complete.length === 0 && errored.length === 0 && (
        <p data-testid="lhrad-findings-empty" style={styles.hint}>
          No AI findings for this study.
        </p>
      )}
      {complete.map((f, i) => (
        <FindingBanner key={`c-${i}`} finding={f} />
      ))}
      {errored.map((f, i) => (
        <FindingError key={`e-${i}`} finding={f} />
      ))}
    </>
  );
}

const FindingBanner: React.FC<{ finding: FindingItem }> = ({ finding }) => {
  const confidenceText =
    finding.confidence != null ? ` (p=${finding.confidence.toFixed(2)})` : '';
  return (
    <div
      data-testid="lhrad-finding-complete"
      data-tool-id={finding.toolId}
      style={styles.completeBanner}
    >
      <div style={styles.bannerHeader}>{finding.toolId}</div>
      <div style={styles.bannerLabel}>
        {finding.label}
        {confidenceText}
      </div>
      <div style={styles.bannerFootnote}>
        Screening signal only — not a radiologist read.
      </div>
    </div>
  );
};

const FindingError: React.FC<{ finding: FindingItem }> = ({ finding }) => (
  <div
    data-testid="lhrad-finding-error"
    data-tool-id={finding.toolId}
    style={styles.errorBanner}
  >
    <div style={styles.bannerHeader}>{finding.toolId}</div>
    <div style={styles.bannerLabel}>AI scan incomplete for this study.</div>
  </div>
);

function readStudyUidFromLocation(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    // OHIF viewer route uses StudyInstanceUIDs as CSV (compare mode supports multiple).
    // We take the first entry and treat that as "the" study for this panel.
    const raw = new URLSearchParams(window.location.search).get(STUDY_URL_PARAM);
    if (!raw) return null;
    return raw.split(',')[0] || null;
  } catch {
    return null;
  }
}

const styles: Record<string, React.CSSProperties> = {
  // Modal-friendly: fixed width, comfortable minimum height, own padding rather than
  // relying on the modal to provide it. Matches OHIF's dark theme so the modal reads
  // as native rather than an iframe.
  panel: {
    minWidth: 340,
    maxWidth: 480,
    minHeight: 120,
    padding: 4,
    color: '#e8eef3',
    fontFamily: 'sans-serif',
    fontSize: 14,
  },
  hint: {
    margin: '4px 0',
    fontSize: '0.85em',
    opacity: 0.6,
    lineHeight: 1.4,
  },
  completeBanner: {
    background: '#2f6cb3',
    color: '#fff',
    padding: '10px 12px',
    marginBottom: 10,
    borderRadius: 4,
    borderLeft: '4px solid #ffcf3f',
  },
  errorBanner: {
    background: '#3a3f47',
    color: '#c8ced5',
    padding: '10px 12px',
    marginBottom: 10,
    borderRadius: 4,
    borderLeft: '4px solid #8a95a0',
    opacity: 0.85,
  },
  bannerHeader: {
    fontSize: '0.75em',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    opacity: 0.8,
    marginBottom: 4,
  },
  bannerLabel: {
    fontSize: '0.95em',
    fontWeight: 600,
    lineHeight: 1.3,
  },
  bannerFootnote: {
    fontSize: '0.75em',
    opacity: 0.75,
    marginTop: 6,
    fontStyle: 'italic',
  },
};
