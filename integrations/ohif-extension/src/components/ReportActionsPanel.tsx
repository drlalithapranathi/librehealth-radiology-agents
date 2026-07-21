/**
 * ReportActionsPanel — right-side viewer panel offering actions on the study
 * the radiologist is currently reading.
 *
 * Today: one action — "Report this study" — opens the RIS report authoring page
 * in a new tab, accession-parameterized so the radiologist lands directly on
 * the report for the study they were reading. Closes #73 item 2.
 *
 * PLACEHOLDER URL: the exact URL pattern for the LibreHealth Radiology o3
 * module's report authoring page is not confirmed at MR-open time; it is
 * flagged for Pranathi's confirmation in the MR description. The template
 * below is intentionally the shape most likely to be correct against the
 * running dev stack, but a deployment CAN override via a global set on the
 * OHIF window shim (`window.LHRAD_RIS_REPORT_URL_TEMPLATE`) so the URL is a
 * config concern, not a code concern. When Pranathi confirms the real
 * pattern, the default here is a one-line change.
 *
 * Why a global rather than an env var: the OHIF viewer is a static SPA served
 * by nginx; there is no build-time env pipe from the compose file into the
 * bundle. The convention across other extensions is to pin runtime config via
 * globals set on `app-config.js` before the SPA loads. Same pattern here.
 *
 * Rendered as a panel, not a toolbar button: (a) it keeps discoverability
 * distinct from the DICOM cine/measurement tools OHIF puts in the main
 * toolbar, and (b) it leaves room for follow-up actions (e.g., "escalate",
 * "flag for teaching") without cluttering the toolbar. This panel slot was
 * previously occupied by PriorsPanel — see #73 item 3 discussion for why
 * PriorsPanel is unregistered pending backing endpoint.
 */
import * as React from 'react';

/** Overridable at runtime via `window.LHRAD_RIS_REPORT_URL_TEMPLATE`. The
 *  `{accession}` token is substituted; anything else stays as-is. When no
 *  accession is present in the URL (unusual — the WorkList row click passes
 *  it explicitly), the button is disabled so we never open a broken link. */
const DEFAULT_RIS_REPORT_URL_TEMPLATE =
  '/openmrs/owa/radiologyapp/index.html#/studies?accession={accession}';

const ACCESSION_URL_PARAM = 'accession';

declare global {
  interface Window {
    LHRAD_RIS_REPORT_URL_TEMPLATE?: string;
  }
}

export interface ReportActionsPanelProps {
  /** Overridable for tests. Reads window.location.search by default. */
  accession?: string | null;
  /** Overridable for tests. Reads window.LHRAD_RIS_REPORT_URL_TEMPLATE or falls back. */
  urlTemplate?: string;
  /** Overridable for tests — normally window.open. */
  openImpl?: (url: string, target: string) => void;
}

export const ReportActionsPanel: React.FC<ReportActionsPanelProps> = ({
  accession,
  urlTemplate,
  openImpl,
}) => {
  const effectiveAccession =
    accession !== undefined ? accession : readAccessionFromLocation();
  const effectiveTemplate =
    urlTemplate ??
    (typeof window !== 'undefined' && window.LHRAD_RIS_REPORT_URL_TEMPLATE) ||
    DEFAULT_RIS_REPORT_URL_TEMPLATE;
  const effectiveOpen =
    openImpl ??
    ((url: string, target: string) => {
      if (typeof window !== 'undefined') {
        window.open(url, target);
      }
    });

  const disabled = !effectiveAccession;
  const targetUrl = effectiveAccession
    ? effectiveTemplate.replace(
        /\{accession\}/g,
        encodeURIComponent(effectiveAccession),
      )
    : '';

  const onReport = () => {
    if (!disabled) {
      // new tab: cross-origin, do not lose the OHIF SPA state
      effectiveOpen(targetUrl, '_blank');
    }
  };

  return (
    <div data-testid="lhrad-report-actions-panel" style={styles.panel}>
      <h3 style={styles.title}>Report Actions</h3>

      <button
        type="button"
        data-testid="lhrad-report-this-study"
        onClick={onReport}
        disabled={disabled}
        style={{
          ...styles.button,
          ...(disabled ? styles.buttonDisabled : {}),
        }}
      >
        Report this study
      </button>

      {disabled && (
        <p data-testid="lhrad-report-actions-hint" style={styles.hint}>
          Open a study from the Reading Worklist to enable reporting.
        </p>
      )}

      {!disabled && (
        <p style={styles.hint}>
          Opens the RIS report authoring page for accession{' '}
          <code style={styles.code}>{effectiveAccession}</code> in a new tab.
        </p>
      )}
    </div>
  );
};

function readAccessionFromLocation(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get(ACCESSION_URL_PARAM);
  } catch {
    return null;
  }
}

const styles: Record<string, React.CSSProperties> = {
  panel: {
    padding: 12,
    color: '#e8eef3',
    fontFamily: 'sans-serif',
    fontSize: 14,
  },
  title: {
    margin: '4px 0 12px',
    fontSize: '1.1em',
    borderBottom: '1px solid #37424c',
    paddingBottom: 6,
  },
  button: {
    display: 'block',
    width: '100%',
    padding: '10px 12px',
    background: '#2f6cb3',
    color: '#fff',
    border: 'none',
    borderRadius: 4,
    fontSize: '0.95em',
    fontWeight: 600,
    cursor: 'pointer',
  },
  buttonDisabled: {
    background: '#2f3841',
    color: '#8a95a0',
    cursor: 'not-allowed',
  },
  hint: {
    marginTop: 10,
    fontSize: '0.85em',
    opacity: 0.6,
    lineHeight: 1.4,
  },
  code: {
    fontFamily: 'monospace',
    background: '#232a31',
    padding: '1px 4px',
    borderRadius: 3,
  },
};
