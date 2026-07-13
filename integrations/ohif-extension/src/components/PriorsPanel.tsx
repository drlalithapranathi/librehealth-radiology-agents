/**
 * PriorsPanel — surfaces EHR-derived priors + overlays alongside the images.
 *
 * Registered via the extension's `getPanelModule` hook so OHIF renders it as a
 * right-side (or left-side; the mode chooses) panel in the viewer layout.
 *
 * Data source contract for M2:
 *   * `?priorsRef=<studyContextRef>` in the URL when the mode is entered.
 *   * A small companion endpoint (not yet built — flagged as a follow-up in
 *     `docs/ohif-integration-approach.md`) resolves the ref to a PriorsPacket.
 *   * Until that endpoint exists, this panel renders a friendly empty state.
 *     Better than an error — the images are what the radiologist is here for;
 *     priors are a nice-to-have layer on top.
 *
 * The panel is intentionally read-only. Any actions (e.g. "flag this prior as
 * relevant") would emit a separate event contract and are out of scope for #21.
 */
import * as React from 'react';
import { useEffect, useState } from 'react';

import type { PriorsPacket } from '../types';

const PRIORS_URL_PARAM = 'priorsRef';

/** Same-origin path — nginx routes /priors-api/* to whichever service resolves
 *  studyContextRefs. Not wired yet in this MR; see the R2 doc's Consequences
 *  section for the intended shape. */
export const PRIORS_API_PATH_PREFIX = '/priors-api/context/';

export interface PriorsPanelProps {
  /** Overridable for tests. Reads window.location.search by default. */
  priorsRef?: string | null;
  /** Overridable for tests. */
  fetchImpl?: typeof fetch;
}

type LoadState =
  | { kind: 'idle' } // no priorsRef yet
  | { kind: 'loading' }
  | { kind: 'ready'; data: PriorsPacket }
  | { kind: 'error'; message: string };

export const PriorsPanel: React.FC<PriorsPanelProps> = ({
  priorsRef,
  fetchImpl = fetch,
}) => {
  // Derive priorsRef from URL if not passed explicitly.
  const effectiveRef =
    priorsRef !== undefined ? priorsRef : readPriorsRefFromLocation();

  const [state, setState] = useState<LoadState>(
    effectiveRef ? { kind: 'loading' } : { kind: 'idle' },
  );

  useEffect(() => {
    if (!effectiveRef) {
      setState({ kind: 'idle' });
      return;
    }
    const controller = new AbortController();
    fetchImpl(`${PRIORS_API_PATH_PREFIX}${encodeURIComponent(effectiveRef)}`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = (await r.json()) as PriorsPacket;
        setState({ kind: 'ready', data });
      })
      .catch((err: Error) => {
        if (err.name === 'AbortError') return;
        setState({ kind: 'error', message: err.message });
      });
    return () => controller.abort();
  }, [effectiveRef, fetchImpl]);

  return (
    <div data-testid="lhrad-priors-panel" style={styles.panel}>
      <h3 style={styles.title}>Priors &amp; Alerts</h3>
      {state.kind === 'idle' && <IdleState />}
      {state.kind === 'loading' && (
        <div data-testid="lhrad-priors-loading" style={styles.muted}>
          Loading priors…
        </div>
      )}
      {state.kind === 'error' && (
        <div data-testid="lhrad-priors-error" style={styles.error}>
          Priors unavailable: {state.message}
        </div>
      )}
      {state.kind === 'ready' && <PriorsView data={state.data} />}
    </div>
  );
};

const IdleState: React.FC = () => (
  <div data-testid="lhrad-priors-idle" style={styles.muted}>
    <p>No priors context linked to this study.</p>
    <p style={styles.hint}>
      Open the study from the Reading Worklist to see priors, active problems, and
      contrast alerts.
    </p>
  </div>
);

const PriorsView: React.FC<{ data: PriorsPacket }> = ({ data }) => {
  return (
    <div data-testid="lhrad-priors-view">
      <ContrastAlerts data={data} />
      <Section title="Prior Studies" empty="No prior imaging on file.">
        {data.priorStudies.length > 0 && (
          <ul style={styles.list}>
            {data.priorStudies.map((p, i) => (
              <li key={`${p.ref}-${i}`} style={styles.li}>
                <span>{p.modality || 'STUDY'}</span>
                {p.date && <span style={styles.dim}> · {p.date}</span>}
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Active Problems" empty="No active problems recorded.">
        {data.activeProblems.length > 0 && (
          <ul style={styles.list}>
            {data.activeProblems.map((c, i) => (
              <li key={`${c.code}-${i}`} style={styles.li}>
                <span>{c.display || c.code}</span>
                <span style={styles.dim}> · {c.code}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Relevant Labs" empty="No labs on file.">
        {data.relevantLabs.length > 0 && (
          <ul style={styles.list}>
            {data.relevantLabs.map((l, i) => (
              <li key={`${l.code}-${i}`} style={styles.li}>
                <span>{l.display || l.code}</span>
                {l.value !== undefined && (
                  <span>
                    : {l.value}
                    {l.unit ? ` ${l.unit}` : ''}
                  </span>
                )}
                {l.date && <span style={styles.dim}> · {l.date}</span>}
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Allergies" empty="No allergies on file.">
        {data.allergies.length > 0 && (
          <ul style={styles.list}>
            {data.allergies.map((a, i) => (
              <li key={`${a.code}-${i}`} style={styles.li}>
                <span>{a.code}</span>
                {a.criticality && (
                  <span style={styles.dim}> · {a.criticality}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
};

/**
 * ContrastAlerts is where the contrastFlags slice earns its keep — three yes/no
 * flags that a radiologist protocolling a study MUST see up front. Rendered as
 * a chip row above the Sections so it grabs attention.
 */
const ContrastAlerts: React.FC<{ data: PriorsPacket }> = ({ data }) => {
  const chips: Array<{ label: string; danger: boolean }> = [];
  if (data.contrastFlags.priorReaction) {
    chips.push({ label: 'Prior contrast reaction', danger: true });
  }
  if (data.contrastFlags.onMetformin) {
    chips.push({ label: 'On metformin', danger: false });
  }
  if (data.contrastFlags.egfr !== null && data.contrastFlags.egfr < 30) {
    chips.push({ label: `eGFR ${data.contrastFlags.egfr}`, danger: true });
  } else if (data.contrastFlags.egfr !== null) {
    chips.push({ label: `eGFR ${data.contrastFlags.egfr}`, danger: false });
  }
  if (chips.length === 0) return null;
  return (
    <div data-testid="lhrad-priors-alerts" style={styles.chipRow}>
      {chips.map((c, i) => (
        <span
          key={i}
          data-danger={c.danger || undefined}
          style={{ ...styles.chip, ...(c.danger ? styles.chipDanger : {}) }}
        >
          {c.label}
        </span>
      ))}
    </div>
  );
};

const Section: React.FC<{
  title: string;
  empty: string;
  children?: React.ReactNode;
}> = ({ title, empty, children }) => (
  <div style={styles.section}>
    <h4 style={styles.sectionTitle}>{title}</h4>
    {children || <p style={styles.muted}>{empty}</p>}
  </div>
);

function readPriorsRefFromLocation(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get(PRIORS_URL_PARAM);
  } catch {
    return null;
  }
}

const styles: Record<string, React.CSSProperties> = {
  panel: { padding: 12, color: '#e8eef3', fontFamily: 'sans-serif', fontSize: 14 },
  title: { margin: '4px 0 12px', fontSize: '1.1em', borderBottom: '1px solid #37424c', paddingBottom: 6 },
  section: { marginBottom: 16 },
  sectionTitle: { margin: '8px 0 6px', fontSize: '0.85em', textTransform: 'uppercase', letterSpacing: 0.5, opacity: 0.7 },
  list: { listStyle: 'none', padding: 0, margin: 0 },
  li: { padding: '4px 0', borderBottom: '1px solid #232a31' },
  dim: { opacity: 0.6, fontSize: '0.9em' },
  muted: { opacity: 0.6, fontStyle: 'italic', fontSize: '0.9em' },
  hint: { marginTop: 8, fontSize: '0.85em', opacity: 0.5 },
  error: { color: '#ffdada', background: '#5c1f1f', padding: 8, borderRadius: 4 },
  chipRow: { display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 },
  chip: { padding: '3px 8px', borderRadius: 12, background: '#2f3841', fontSize: '0.85em' },
  chipDanger: { background: '#5c1f1f', color: '#ffdada', fontWeight: 600 },
};
