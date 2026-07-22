/**
 * Tests for registerReportButtonOnPrimary — the merge-safe append that puts our button
 * on the primary toolbar section without wiping the mode's own buttons.
 *
 * The tricky bit is that OHIF's createButtonSection('primary', [...ids]) has UPSERT
 * semantics — passing only ['lhrad.report'] would leave the toolbar with only our
 * button, hiding MeasurementTools/Zoom/etc. These tests pin the three paths:
 *   - toolbarService exposes getButtonSection('primary')  -> use the live list
 *   - toolbarService exposes getButtonPropsInSection      -> map to ids and use
 *   - toolbarService exposes neither                      -> fall back to the
 *     hardcoded longitudinal-mode defaults
 */
import { describe, it, expect, vi } from 'vitest';

import {
  LONGITUDINAL_PRIMARY_DEFAULTS,
  REPORT_BUTTON_ID,
  registerReportButtonOnPrimary,
  reportButtonDefinition,
} from '../toolbar/reportButton';


function makeToolbarService(
  overrides: Partial<{
    getButtonSection: (name: string) => string[] | undefined;
    getButtonPropsInSection: (name: string) => Array<{ id: string }> | undefined;
  }> = {},
) {
  return {
    addButtons: vi.fn(),
    createButtonSection: vi.fn(),
    ...overrides,
  };
}


// ===========================================================================
// The core invariant: primary section keeps its existing buttons AND gains ours
// ===========================================================================
describe('registerReportButtonOnPrimary: merge-safe append', () => {
  it('appends the report button to the section returned by getButtonSection', () => {
    const service = makeToolbarService({
      getButtonSection: (name) =>
        name === 'primary' ? ['MeasurementTools', 'Zoom', 'WindowLevel'] : undefined,
    });

    registerReportButtonOnPrimary(service);

    expect(service.addButtons).toHaveBeenCalledWith([reportButtonDefinition]);
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'Zoom',
      'WindowLevel',
      REPORT_BUTTON_ID,
    ]);
  });

  it('maps getButtonPropsInSection output to ids when getButtonSection is absent', () => {
    const service = makeToolbarService({
      getButtonPropsInSection: (name) =>
        name === 'primary'
          ? [{ id: 'MeasurementTools' }, { id: 'Pan' }, { id: 'Layout' }]
          : undefined,
    });

    registerReportButtonOnPrimary(service);

    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'Pan',
      'Layout',
      REPORT_BUTTON_ID,
    ]);
  });

  it('falls back to the canonical longitudinal defaults when no getter is exposed', () => {
    const service = makeToolbarService(); // no getButtonSection, no getButtonPropsInSection

    registerReportButtonOnPrimary(service);

    const expected = [...LONGITUDINAL_PRIMARY_DEFAULTS, REPORT_BUTTON_ID];
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', expected);
  });

  it('falls back to defaults if the getter throws', () => {
    const service = makeToolbarService({
      getButtonSection: () => {
        throw new Error('service not ready');
      },
    });

    registerReportButtonOnPrimary(service);

    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      ...LONGITUDINAL_PRIMARY_DEFAULTS,
      REPORT_BUTTON_ID,
    ]);
  });

  it('falls back to defaults if the getter returns an empty list', () => {
    // A fresh toolbarService before any mode has populated primary. Rather than
    // creating a primary section with ONLY our button (which would hide MeasurementTools
    // etc. once the mode's buttons register), we lay down the canonical defaults + ours.
    const service = makeToolbarService({
      getButtonSection: () => [],
    });

    registerReportButtonOnPrimary(service);

    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      ...LONGITUDINAL_PRIMARY_DEFAULTS,
      REPORT_BUTTON_ID,
    ]);
  });
});


// ===========================================================================
// Idempotency: safe to call twice (e.g., mode switch and re-enter)
// ===========================================================================
describe('registerReportButtonOnPrimary: idempotency', () => {
  it('does not double-append the button id if it is already in the primary list', () => {
    const service = makeToolbarService({
      getButtonSection: () => ['MeasurementTools', 'Zoom', REPORT_BUTTON_ID],
    });

    registerReportButtonOnPrimary(service);

    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'Zoom',
      REPORT_BUTTON_ID,
    ]);
  });

  it('addButtons is called every time (button definition upsert is safe)', () => {
    const service = makeToolbarService({
      getButtonSection: () => ['MeasurementTools'],
    });

    registerReportButtonOnPrimary(service);
    registerReportButtonOnPrimary(service);

    // Two enters -> two addButtons calls, same definition each time (upsert semantics
    // in OHIF's toolbarService; safe).
    expect(service.addButtons).toHaveBeenCalledTimes(2);
    expect(service.createButtonSection).toHaveBeenCalledTimes(2);
  });
});


// ===========================================================================
// Button definition contract
// ===========================================================================
describe('reportButtonDefinition', () => {
  it('references the openReportForStudy command by id', () => {
    expect(reportButtonDefinition.id).toBe(REPORT_BUTTON_ID);
    expect(reportButtonDefinition.props.commands).toEqual([
      { commandName: 'lhrad.openReportForStudy' },
    ]);
  });

  it('is always enabled (command handles no-accession by opening dashboard fallback)', () => {
    const result = reportButtonDefinition.props.evaluate();
    expect(result.disabled).toBe(false);
  });
});
