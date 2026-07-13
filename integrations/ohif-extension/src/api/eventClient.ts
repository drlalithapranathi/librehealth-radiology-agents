/**
 * StudyOpenedEvent emitter — POSTs to the orchestrator's ingest surface when the
 * radiologist opens a study. Wire shape matches `contracts/events/ohif-opened.schema.json`.
 *
 * BEST-EFFORT by design, same philosophy as `radagent_common.worklist_client.publish_priority`:
 *   * losing an ohif.study.opened event = losing pre-read assist for that study, NOT
 *     losing the ability to read/report/sign the study
 *   * so a network failure logs a warning and returns false rather than blocking
 *     the mode entry (which would prevent the radiologist from actually opening the study)
 *
 * The orchestrator's `orthanc_webhook` ingest surface in `orchestrator/ingress.py` is
 * the model for the sibling endpoint we POST to; wiring that endpoint is flagged as
 * ~10 lines of follow-up in the R2 doc (`docs/ohif-integration-approach.md`).
 * Until it exists, this helper still runs cleanly — the fetch just gets a 404
 * and we log at WARNING level, matching the "publish is visibility, not correctness"
 * pattern the orchestrator side already uses.
 */
import type { StudyOpenedEvent } from '../types';

/** Same-origin path — nginx routes /orchestrator-api/* to the orchestrator ingress. */
export const EVENT_PATH = '/orchestrator-api/events/ohif-opened';

export interface EmitOptions {
  radiologistId?: string;
  /** For tests; defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Same-origin path override — production deployments may proxy events differently. */
  url?: string;
  /** Short timeout — this call is on the mode-entry path; a slow ingest should
   *  not stall the viewer from loading. 5 s matches the Worklist API's own
   *  publish_priority timeout for symmetry. */
  timeoutMs?: number;
}

/**
 * Emit `ohif.study.opened` for the given study. Returns `true` on 2xx, `false`
 * on any error (network, timeout, non-2xx). Never throws — the caller (mode
 * onModeEnter) is on the critical UI path and cannot fail on this.
 */
export async function emitStudyOpenedEvent(
  studyInstanceUID: string,
  options: EmitOptions = {},
): Promise<boolean> {
  const {
    radiologistId,
    fetchImpl = fetch,
    url = EVENT_PATH,
    timeoutMs = 5000,
  } = options;

  const event: StudyOpenedEvent = {
    schemaVersion: '1.0.0',
    eventType: 'ohif.study.opened',
    studyInstanceUID,
    openedAt: new Date().toISOString(),
    ...(radiologistId ? { radiologistId } : {}),
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
      signal: controller.signal,
    });
    if (!response.ok) {
      // eslint-disable-next-line no-console
      console.warn(
        `[lhrad] StudyOpenedEvent POST rejected ${response.status} for study=${studyInstanceUID}`,
      );
      return false;
    }
    return true;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      `[lhrad] StudyOpenedEvent POST failed for study=${studyInstanceUID}:`,
      err,
    );
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Build a same-origin OHIF viewer URL for a given StudyInstanceUID.
 * OHIF's viewer route accepts `?StudyInstanceUIDs=<uid>` as a query parameter
 * (multi-value CSV supported in v3 for compare mode; we always pass one).
 * The route is `/viewer` per OHIF v3's default mode registration; if a deployer
 * changes routerBasename in `app-config.js`, this helper is the one place to
 * update.
 */
export function buildViewerUrl(studyInstanceUID: string): string {
  const params = new URLSearchParams({ StudyInstanceUIDs: studyInstanceUID });
  return `/viewer?${params.toString()}`;
}
