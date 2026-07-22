/**
 * openReportForStudy — the "Report this study" affordance's core logic, extracted from
 * ReportActionsPanel so it can be driven from the toolbar button (#73 criterion 1
 * follow-up per Saptarshi's !95 drill) OR from the panel component (retained for tests
 * + any future OHIF version where extension panels do mount in the default mode).
 *
 * OHIF 3.6's default mode does NOT mount extension panels — Saptarshi's live browser
 * probe on !95 confirmed the right panel bar offers only Segmentation/Measurements.
 * The affordance therefore lives on the toolbar, wired via getCommandsModule +
 * getToolbarModule + onModeEnter in `src/index.ts`. This module is the shared
 * implementation the command invokes.
 *
 * Flow:
 *   1. Read accession from window.location.search (`?accession=<num>` set by WorkList).
 *   2. If no accession OR the override template carries `{accession}` — substitute
 *      directly (fallback path / operator override path, no resolver needed).
 *   3. Otherwise resolve accession -> order UUID via /openmrs/ws/rest/v1/radiologyorder,
 *      same-origin under the RIS session (nginx /openmrs/ proxy landed in !95).
 *   4. Substitute `{orderUuid}` into the URL template; open in a new tab.
 *   5. Any failure -> RIS orders dashboard fallback. Never a dead click.
 */

/** Overridable at runtime via `window.LHRAD_RIS_REPORT_URL_TEMPLATE`. A `{orderUuid}`
 *  token triggers accession -> order resolution first; an `{accession}` token substitutes
 *  directly with no lookup. Default URL confirmed against the live o3 stack in Saptarshi's
 *  !95 browser drill. */
export const DEFAULT_RIS_REPORT_URL_TEMPLATE =
  '/openmrs/module/radiology/radiologyOrder.form?orderId={orderUuid}';

/** Radiology module's accession index; same-origin via the nginx /openmrs/ proxy so the
 *  radiologist's existing RIS session cookie authenticates it. */
export const RESOLVE_ORDER_PATH =
  '/openmrs/ws/rest/v1/radiologyorder?v=custom:(uuid)&accessionNumber=';

/** Landing page when resolution fails: the RIS orders dashboard. OpenMRS itself bounces
 *  an unauthenticated hit through login and back — never a dead click. */
export const RIS_FALLBACK_URL =
  '/openmrs/module/radiology/radiologyDashboardOrdersTab.htm';

const ACCESSION_URL_PARAM = 'accession';

declare global {
  interface Window {
    LHRAD_RIS_REPORT_URL_TEMPLATE?: string;
  }
}

export interface OpenReportOptions {
  /** Overridable for tests. Reads window.location.search by default. */
  accession?: string | null;
  /** Overridable for tests. Reads window.LHRAD_RIS_REPORT_URL_TEMPLATE or falls back. */
  urlTemplate?: string;
  /** Overridable for tests — normally window.open. */
  openImpl?: (url: string, target: string) => void;
  /** Overridable for tests — normally global fetch. */
  fetchImpl?: typeof fetch;
}

/**
 * Do the "Report this study" affordance's work. Returns the URL that was opened, so tests
 * can assert without stubbing window.open (though openImpl is the preferred injection point).
 * Returns `null` if the caller had no accession AND the template does not carry `{accession}` —
 * i.e., nothing to do beyond warning; a fallback URL is still opened.
 */
export async function openReportForStudy(
  options: OpenReportOptions = {},
): Promise<string> {
  const accession =
    options.accession !== undefined ? options.accession : readAccessionFromLocation();
  const template =
    options.urlTemplate ??
    ((typeof window !== 'undefined' && window.LHRAD_RIS_REPORT_URL_TEMPLATE) ||
      DEFAULT_RIS_REPORT_URL_TEMPLATE);
  const open =
    options.openImpl ??
    ((url: string, target: string) => {
      if (typeof window !== 'undefined') window.open(url, target);
    });
  const doFetch =
    options.fetchImpl ?? (typeof fetch !== 'undefined' ? fetch : undefined);

  // No accession AND the template does not substitute one — land on the dashboard so the
  // radiologist can navigate manually. This is the "viewer opened without going through
  // the worklist" path (direct-link, browser back button, etc.).
  if (!accession && !template.includes('{accession}')) {
    open(RIS_FALLBACK_URL, '_blank');
    return RIS_FALLBACK_URL;
  }

  // Operator override with `{accession}` — substitute directly, no resolver call.
  if (accession && template.includes('{accession}')) {
    const url = template.replace(/\{accession\}/g, encodeURIComponent(accession));
    open(url, '_blank');
    return url;
  }

  // Default path: resolve accession -> orderUuid, substitute into template.
  let target = RIS_FALLBACK_URL;
  if (accession && doFetch) {
    try {
      const res = await doFetch(RESOLVE_ORDER_PATH + encodeURIComponent(accession), {
        credentials: 'include',
      });
      if (res.ok) {
        const body = await res.json();
        const uuid = body?.results?.[0]?.uuid;
        if (uuid) {
          target = template.replace(/\{orderUuid\}/g, encodeURIComponent(uuid));
        }
      }
    } catch {
      // fall through to the dashboard — never a dead click
    }
  }
  open(target, '_blank');
  return target;
}

function readAccessionFromLocation(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get(ACCESSION_URL_PARAM);
  } catch {
    return null;
  }
}
