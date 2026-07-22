import { describe, expect, it, vi } from 'vitest';

import {
  buildViewerUrl,
  emitStudyOpenedEvent,
  EVENT_PATH,
  isChestRadiograph,
} from '../api/eventClient';

// --- buildViewerUrl ---------------------------------------------------------

describe('buildViewerUrl', () => {
  it('constructs same-origin /viewer URL with StudyInstanceUIDs param', () => {
    expect(buildViewerUrl('1.2.3.4')).toBe('/read?StudyInstanceUIDs=1.2.3.4');
  });

  it('URL-encodes exotic UIDs (defense-in-depth even though DICOM UIDs are ASCII)', () => {
    // Real DICOM UIDs won't have these, but the encoding contract must be honored.
    expect(buildViewerUrl('1.2 3')).toBe('/read?StudyInstanceUIDs=1.2+3');
  });
});

// --- emitStudyOpenedEvent --------------------------------------------------

describe('emitStudyOpenedEvent', () => {
  it('POSTs an ohif.study.opened event with the expected schema shape', async () => {
    let seenUrl = '';
    let seenBody: unknown = null;
    let seenMethod = '';
    let seenContentType = '';
    const fetchImpl = async (url: RequestInfo | URL, init?: RequestInit) => {
      seenUrl = String(url);
      seenMethod = init?.method || '';
      seenContentType = (init?.headers as Record<string, string> | undefined)?.['Content-Type'] ?? '';
      seenBody = JSON.parse((init?.body as string) || '{}');
      return new Response('', { status: 204 });
    };

    const ok = await emitStudyOpenedEvent('1.2.3', { fetchImpl });
    expect(ok).toBe(true);
    expect(seenUrl).toBe(EVENT_PATH);
    expect(seenMethod).toBe('POST');
    expect(seenContentType).toBe('application/json');
    expect(seenBody).toMatchObject({
      schemaVersion: '1.0.0',
      eventType: 'ohif.study.opened',
      studyInstanceUID: '1.2.3',
    });
    // openedAt must be a valid ISO-8601 timestamp
    expect(typeof (seenBody as any).openedAt).toBe('string');
    expect(() => new Date((seenBody as any).openedAt).toISOString()).not.toThrow();
  });

  it('includes radiologistId when provided', async () => {
    let seenBody: any = null;
    const fetchImpl = async (_url: RequestInfo | URL, init?: RequestInit) => {
      seenBody = JSON.parse((init?.body as string) || '{}');
      return new Response('', { status: 204 });
    };
    await emitStudyOpenedEvent('1.2.3', { fetchImpl, radiologistId: 'rad-42' });
    expect(seenBody.radiologistId).toBe('rad-42');
  });

  it('OMITS radiologistId when not provided (rather than sending null)', async () => {
    let seenBody: any = null;
    const fetchImpl = async (_url: RequestInfo | URL, init?: RequestInit) => {
      seenBody = JSON.parse((init?.body as string) || '{}');
      return new Response('', { status: 204 });
    };
    await emitStudyOpenedEvent('1.2.3', { fetchImpl });
    expect(seenBody).not.toHaveProperty('radiologistId');
  });

  it('swallows network errors and returns false (best-effort semantics)', async () => {
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchImpl = async () => {
      throw new TypeError('Failed to fetch');
    };
    const ok = await emitStudyOpenedEvent('1.2.3', { fetchImpl });
    expect(ok).toBe(false);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('swallows non-2xx responses and returns false', async () => {
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchImpl = async () => new Response('', { status: 404 });
    const ok = await emitStudyOpenedEvent('1.2.3', { fetchImpl });
    expect(ok).toBe(false);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('treats any 2xx as success (204 today; 200 tomorrow)', async () => {
    for (const status of [200, 201, 202, 204]) {
      const fetchImpl = async () => new Response('', { status });
      const ok = await emitStudyOpenedEvent('1.2.3', { fetchImpl });
      expect(ok).toBe(true);
    }
  });
});

describe('buildViewerUrl hanging protocol selection (#73 item 4)', () => {
  it('appends hangingProtocolId for a CR chest radiograph', () => {
    const url = buildViewerUrl('1.2.3', 'ACC1', {
      modality: 'CR',
      studyDescription: 'XR CHEST PA AND LATERAL',
    });
    expect(url).toContain('hangingProtocolId=lhrad.cxr.two-view');
  });

  it('modality is authoritative: DX and CX hang, CT never does even with CHEST in the name', () => {
    expect(isChestRadiograph('DX', 'CHEST 2 VIEWS')).toBe(true);
    expect(isChestRadiograph('CX', '')).toBe(true);
    expect(isChestRadiograph('CT', 'CT CHEST WITH CONTRAST')).toBe(false);
  });

  it('with no modality, only a radiograph-shaped chest description hangs', () => {
    expect(isChestRadiograph('', 'XR CHEST PA AND LATERAL')).toBe(true);
    expect(isChestRadiograph(undefined, 'CXR PORTABLE')).toBe(true);
    expect(isChestRadiograph('', 'CT CHEST WITH CONTRAST')).toBe(false);
    expect(isChestRadiograph('', '')).toBe(false);
  });

  it('no study info leaves the URL untouched (backwards compatible)', () => {
    expect(buildViewerUrl('1.2.3', 'ACC1')).toBe(
      '/read?StudyInstanceUIDs=1.2.3&accession=ACC1',
    );
  });
});
