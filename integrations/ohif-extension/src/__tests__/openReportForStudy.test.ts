/**
 * Tests for openReportForStudy — the "Report this study" affordance's core logic.
 *
 * These exercise the resolve-accession-to-order-uuid path, the operator-override path
 * (`{accession}` in template), and the fallback path (no accession, or lookup fails).
 * All fetch + window.open are injected so the tests never touch real network or DOM.
 */
import { describe, it, expect, vi } from 'vitest';

import {
  DEFAULT_RIS_REPORT_URL_TEMPLATE,
  RESOLVE_ORDER_PATH,
  RIS_FALLBACK_URL,
  openReportForStudy,
} from '../commands/openReportForStudy';


function makeMocks() {
  const openImpl = vi.fn<(url: string, target: string) => void>();
  return { openImpl };
}


// ===========================================================================
// Happy path: resolve accession -> orderUuid, substitute into default template
// ===========================================================================
describe('openReportForStudy: default resolver path', () => {
  it('resolves accession -> orderUuid and opens the substituted URL', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(async (url: string) => {
      expect(url).toContain(RESOLVE_ORDER_PATH);
      expect(url).toContain('ACC12345');
      return new Response(
        JSON.stringify({ results: [{ uuid: 'abc-123-uuid' }] }),
        { status: 200 },
      );
    }) as unknown as typeof fetch;

    const target = await openReportForStudy({
      accession: 'ACC12345',
      openImpl,
      fetchImpl,
    });

    expect(target).toBe(
      '/openmrs/module/radiology/radiologyOrder.form?orderId=abc-123-uuid',
    );
    expect(openImpl).toHaveBeenCalledWith(target, '_blank');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('falls back to the RIS dashboard when the resolver returns no results', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(
      async () => new Response(JSON.stringify({ results: [] }), { status: 200 }),
    ) as unknown as typeof fetch;

    const target = await openReportForStudy({
      accession: 'ACC-NOT-FOUND',
      openImpl,
      fetchImpl,
    });

    expect(target).toBe(RIS_FALLBACK_URL);
    expect(openImpl).toHaveBeenCalledWith(RIS_FALLBACK_URL, '_blank');
  });

  it('falls back when the resolver returns non-2xx (session expired, RIS down)', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(
      async () => new Response('unauthorized', { status: 401 }),
    ) as unknown as typeof fetch;

    const target = await openReportForStudy({
      accession: 'ACC12345',
      openImpl,
      fetchImpl,
    });

    expect(target).toBe(RIS_FALLBACK_URL);
  });

  it('falls back when fetch throws (network down)', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(async () => {
      throw new Error('network unreachable');
    }) as unknown as typeof fetch;

    const target = await openReportForStudy({
      accession: 'ACC12345',
      openImpl,
      fetchImpl,
    });

    expect(target).toBe(RIS_FALLBACK_URL);
  });

  it('sends credentials: "include" so the RIS session cookie authenticates the lookup', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.credentials).toBe('include');
      return new Response(JSON.stringify({ results: [{ uuid: 'u1' }] }), { status: 200 });
    }) as unknown as typeof fetch;

    await openReportForStudy({ accession: 'A1', openImpl, fetchImpl });
  });
});


// ===========================================================================
// Operator override with `{accession}` — skip resolver entirely
// ===========================================================================
describe('openReportForStudy: {accession} override', () => {
  it('substitutes {accession} directly without calling fetch', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(async () => {
      throw new Error('fetch should not be called');
    }) as unknown as typeof fetch;

    const target = await openReportForStudy({
      accession: 'ACC12345',
      urlTemplate: '/some/other/ris/page?acc={accession}',
      openImpl,
      fetchImpl,
    });

    expect(target).toBe('/some/other/ris/page?acc=ACC12345');
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('URL-encodes the accession in the substitution', async () => {
    const { openImpl } = makeMocks();
    const target = await openReportForStudy({
      accession: 'ACC/12345', // slash needs encoding
      urlTemplate: '/x?acc={accession}',
      openImpl,
    });
    expect(target).toBe('/x?acc=ACC%2F12345');
  });
});


// ===========================================================================
// No accession — dashboard fallback, never a dead click
// ===========================================================================
describe('openReportForStudy: no accession', () => {
  it('opens the RIS dashboard when no accession is available', async () => {
    const { openImpl } = makeMocks();
    const target = await openReportForStudy({
      accession: null,
      openImpl,
    });
    expect(target).toBe(RIS_FALLBACK_URL);
    expect(openImpl).toHaveBeenCalledWith(RIS_FALLBACK_URL, '_blank');
  });

  it('resolver is not called when accession is absent', async () => {
    const { openImpl } = makeMocks();
    const fetchImpl = vi.fn(async () => {
      throw new Error('should not fetch without accession');
    }) as unknown as typeof fetch;

    await openReportForStudy({ accession: null, openImpl, fetchImpl });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});


// ===========================================================================
// Default template constant contract
// ===========================================================================
describe('DEFAULT_RIS_REPORT_URL_TEMPLATE', () => {
  it('is the /openmrs/module/radiology/radiologyOrder.form path Saptarshi confirmed live', () => {
    // Guard against silent drift of the default. If this changes, the change is
    // deliberate and should be reviewed against the running dev stack (Saptarshi's
    // !95 drill established this URL).
    expect(DEFAULT_RIS_REPORT_URL_TEMPLATE).toBe(
      '/openmrs/module/radiology/radiologyOrder.form?orderId={orderUuid}',
    );
  });

  it('uses {orderUuid} (triggers resolver), not {accession} (direct substitution)', () => {
    expect(DEFAULT_RIS_REPORT_URL_TEMPLATE).toContain('{orderUuid}');
    expect(DEFAULT_RIS_REPORT_URL_TEMPLATE).not.toContain('{accession}');
  });
});
