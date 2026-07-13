/**
 * WorkList component render tests.
 *
 * Verifies the pieces that don't depend on OHIF being present:
 *   * loads via injected fetch, renders rows in priority order
 *   * error state renders when Worklist API returns 503
 *   * empty state renders when Worklist API returns 0 items
 *   * clicking a row calls onOpenStudy with the right UID
 *   * `data-priority-tier` attribute is set so visual styling can key off it
 *
 * Not covered here (deferred to Docker smoke test):
 *   * OHIF's customRoutes extension point mounts this at /reading (see index.ts preRegistration)
 *   * The 30 s refresh interval — asserting on timers with happy-dom is finicky
 *     and the risk/reward isn't there for a first MR
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import { WorkList } from '../components/WorkList';
import type { WorklistItem } from '../types';

const item = (overrides: Partial<WorklistItem> = {}): WorklistItem => ({
  orthancStudyId: 'o-1',
  studyInstanceUID: 'uid-1',
  accessionNumber: 'ACC-1',
  modality: 'CT',
  studyDescription: 'CT CHEST',
  studyDate: '20260710',
  numberOfInstances: 100,
  priorityTier: 'ROUTINE',
  priorityScore: 50,
  workflowId: null,
  assignment: null,
  ...overrides,
});

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status });

// Patch global fetch for each test.
const withFetch = (
  handler: (url: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) => {
  vi.stubGlobal('fetch', vi.fn(handler));
};

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe('<WorkList />', () => {
  it('renders loading state initially', () => {
    withFetch(async () => new Promise(() => {})); // never resolves
    render(<WorkList />);
    expect(screen.getByTestId('lhrad-worklist-loading')).toBeInTheDocument();
  });

  it('renders rows in priority order (STAT > URGENT > ROUTINE)', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: '2026-07-10T00:00Z',
        items: [
          item({ studyInstanceUID: 'routine', priorityTier: 'ROUTINE', priorityScore: 40 }),
          item({ studyInstanceUID: 'urgent', priorityTier: 'URGENT', priorityScore: 70 }),
          item({ studyInstanceUID: 'stat', priorityTier: 'STAT', priorityScore: 95 }),
        ],
      }),
    );
    render(<WorkList />);
    await waitFor(() => expect(screen.queryByTestId('lhrad-worklist-loading')).not.toBeInTheDocument());
    const rows = screen.getAllByRole('row').slice(1); // skip <thead>
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'lhrad-row-stat',
      'lhrad-row-urgent',
      'lhrad-row-routine',
    ]);
  });

  it('sets data-priority-tier so styling can key off it', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 's', priorityTier: 'STAT', priorityScore: 90 })],
      }),
    );
    render(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-row-s'));
    expect(screen.getByTestId('lhrad-row-s')).toHaveAttribute('data-priority-tier', 'STAT');
  });

  it('renders empty state for zero-item response', async () => {
    withFetch(async () => jsonResponse({ generatedAt: 't', items: [] }));
    render(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-worklist-empty'));
    expect(screen.getByTestId('lhrad-worklist-empty')).toHaveTextContent(
      /no studies pending/i,
    );
  });

  it('renders error banner (loud, not empty list) on 503', async () => {
    withFetch(async () => jsonResponse({ detail: 'orthanc unreachable' }, 503));
    render(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-worklist-error'));
    expect(screen.getByTestId('lhrad-worklist-error')).toHaveTextContent(/503/);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('calls onOpenStudy with the row UID when the row is clicked', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 'clickme' })],
      }),
    );
    const onOpenStudy = vi.fn();
    render(<WorkList onOpenStudy={onOpenStudy} />);
    await waitFor(() => screen.getByTestId('lhrad-row-clickme'));
    fireEvent.click(screen.getByTestId('lhrad-row-clickme'));
    expect(onOpenStudy).toHaveBeenCalledWith('clickme');
  });

  it('opens on keyboard Enter (accessibility)', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 'kb' })],
      }),
    );
    const onOpenStudy = vi.fn();
    render(<WorkList onOpenStudy={onOpenStudy} />);
    await waitFor(() => screen.getByTestId('lhrad-row-kb'));
    fireEvent.keyDown(screen.getByTestId('lhrad-row-kb'), { key: 'Enter' });
    expect(onOpenStudy).toHaveBeenCalledWith('kb');
  });
});

// Ambient afterEach/beforeEach come from Vitest globals config.
declare function afterEach(fn: () => void): void;
