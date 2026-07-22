/**
 * ReportActionsPanel — the "Report this study" deep link (#73 items 1+2).
 *
 * The contract these tests pin (live-verified against the o3 stack 2026-07-22):
 *   * default flow resolves accession -> order uuid through the radiology
 *     module REST and opens the RIS order page with that uuid;
 *   * resolution failure (network, non-2xx, empty result) opens the RIS orders
 *     dashboard instead — the button is never a dead click;
 *   * an operator template carrying {accession} substitutes directly and makes
 *     NO lookup (the configurable-override contract from the first cut).
 */
import { describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import { ReportActionsPanel } from '../components/ReportActionsPanel';

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('<ReportActionsPanel />', () => {
  it('resolves the accession to the order uuid and opens the RIS order page', async () => {
    const openImpl = vi.fn();
    const fetchImpl = vi.fn(async (url: RequestInfo | URL) => {
      expect(String(url)).toContain(
        '/openmrs/ws/rest/v1/radiologyorder?v=custom:(uuid)&accessionNumber=ACC1',
      );
      return jsonResponse({ results: [{ uuid: 'uuid-123' }] });
    });
    render(
      <ReportActionsPanel accession="ACC1" openImpl={openImpl} fetchImpl={fetchImpl} />,
    );
    fireEvent.click(screen.getByTestId('lhrad-report-this-study'));
    await waitFor(() =>
      expect(openImpl).toHaveBeenCalledWith(
        '/openmrs/module/radiology/radiologyOrder.form?orderId=uuid-123',
        '_blank',
      ),
    );
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ credentials: 'include' });
  });

  it('falls back to the RIS orders dashboard when the lookup finds nothing', async () => {
    const openImpl = vi.fn();
    const fetchImpl = vi.fn(async () => jsonResponse({ results: [] }));
    render(
      <ReportActionsPanel accession="ACC1" openImpl={openImpl} fetchImpl={fetchImpl} />,
    );
    fireEvent.click(screen.getByTestId('lhrad-report-this-study'));
    await waitFor(() =>
      expect(openImpl).toHaveBeenCalledWith(
        '/openmrs/module/radiology/radiologyDashboardOrdersTab.htm',
        '_blank',
      ),
    );
  });

  it('falls back to the dashboard when the lookup throws (RIS down, not logged in)', async () => {
    const openImpl = vi.fn();
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });
    render(
      <ReportActionsPanel accession="ACC1" openImpl={openImpl} fetchImpl={fetchImpl} />,
    );
    fireEvent.click(screen.getByTestId('lhrad-report-this-study'));
    await waitFor(() =>
      expect(openImpl).toHaveBeenCalledWith(
        '/openmrs/module/radiology/radiologyDashboardOrdersTab.htm',
        '_blank',
      ),
    );
  });

  it('an {accession} operator template substitutes directly with no lookup', async () => {
    const openImpl = vi.fn();
    const fetchImpl = vi.fn();
    render(
      <ReportActionsPanel
        accession="ACC 1"
        urlTemplate="/custom/report?acc={accession}"
        openImpl={openImpl}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.click(screen.getByTestId('lhrad-report-this-study'));
    await waitFor(() =>
      expect(openImpl).toHaveBeenCalledWith('/custom/report?acc=ACC%201', '_blank'),
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('disables the button and shows the hint when no accession is present', () => {
    render(<ReportActionsPanel accession={null} />);
    expect(screen.getByTestId('lhrad-report-this-study')).toBeDisabled();
    expect(screen.getByTestId('lhrad-report-actions-hint')).toBeInTheDocument();
  });
});
