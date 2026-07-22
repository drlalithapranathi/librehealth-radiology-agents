/**
 * Tests for findingsButton — the merge-safe append to the primary toolbar section,
 * the evaluate() icon-state contract, and the module-local icon-state cache the
 * showFindings command pokes when it opens the modal.
 */
import { beforeEach, describe, it, expect, vi } from 'vitest';

import {
  FINDINGS_BUTTON_ID,
  LONGITUDINAL_PRIMARY_DEFAULTS_FOR_FINDINGS,
  findingsButtonDefinition,
  registerFindingsButtonOnPrimary,
  setFindingsIconState,
  _resetFindingsIconStateCacheForTests,
  _setCurrentStudyUidForTests,
} from '../toolbar/findingsButton';


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
// Merge-safe append — the core primary-section invariant
// ===========================================================================
describe('registerFindingsButtonOnPrimary: merge-safe append', () => {
  it('appends the findings button to the existing primary section', () => {
    const service = makeToolbarService({
      getButtonSection: (name) =>
        name === 'primary' ? ['MeasurementTools', 'Zoom', 'lhrad.report'] : undefined,
    });

    registerFindingsButtonOnPrimary(service);

    expect(service.addButtons).toHaveBeenCalledWith([findingsButtonDefinition]);
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'Zoom',
      'lhrad.report',
      FINDINGS_BUTTON_ID,
    ]);
  });

  it('reads getButtonPropsInSection when getButtonSection is absent', () => {
    const service = makeToolbarService({
      getButtonPropsInSection: (name) =>
        name === 'primary'
          ? [{ id: 'MeasurementTools' }, { id: 'Pan' }]
          : undefined,
    });

    registerFindingsButtonOnPrimary(service);

    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'Pan',
      FINDINGS_BUTTON_ID,
    ]);
  });

  it('falls back to canonical longitudinal defaults when no getter is exposed', () => {
    const service = makeToolbarService();
    registerFindingsButtonOnPrimary(service);
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      ...LONGITUDINAL_PRIMARY_DEFAULTS_FOR_FINDINGS,
      FINDINGS_BUTTON_ID,
    ]);
  });

  it('falls back if the getter throws', () => {
    const service = makeToolbarService({
      getButtonSection: () => {
        throw new Error('service not ready');
      },
    });
    registerFindingsButtonOnPrimary(service);
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      ...LONGITUDINAL_PRIMARY_DEFAULTS_FOR_FINDINGS,
      FINDINGS_BUTTON_ID,
    ]);
  });
});


// ===========================================================================
// Idempotency: safe to double-call
// ===========================================================================
describe('registerFindingsButtonOnPrimary: idempotency', () => {
  it('does not double-append if findings button is already in primary', () => {
    const service = makeToolbarService({
      getButtonSection: () => ['MeasurementTools', 'lhrad.report', FINDINGS_BUTTON_ID],
    });
    registerFindingsButtonOnPrimary(service);
    expect(service.createButtonSection).toHaveBeenCalledWith('primary', [
      'MeasurementTools',
      'lhrad.report',
      FINDINGS_BUTTON_ID,
    ]);
  });
});


// ===========================================================================
// Icon state — evaluate() reflects the cache
// ===========================================================================
describe('findingsButtonDefinition.evaluate: icon state', () => {
  beforeEach(() => {
    _resetFindingsIconStateCacheForTests();
  });

  it('returns muted tint by default when no state cached', () => {
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.disabled).toBe(false);
    expect(result.className).toContain('lhrad-findings-button--muted');
  });

  it('reflects a colored tint when setFindingsIconState was called for the current study', () => {
    _setCurrentStudyUidForTests('1.2.3.4');
    setFindingsIconState('1.2.3.4', 'colored');
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.className).toContain('lhrad-findings-button--colored');
  });

  it('reflects gray tint on ERROR', () => {
    _setCurrentStudyUidForTests('uid-with-error');
    setFindingsIconState('uid-with-error', 'gray');
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.className).toContain('lhrad-findings-button--gray');
  });

  it('stays muted when the current study UID differs from the cache', () => {
    _setCurrentStudyUidForTests('other-uid');
    setFindingsIconState('some-cached-uid', 'colored');
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.className).toContain('lhrad-findings-button--muted');
  });

  it('resolves to the current UID via the injectable setter (runtime uses window.location)', () => {
    // At runtime, currentStudyUid() reads window.location.search's StudyInstanceUIDs
    // param (first CSV entry). happy-dom does not update location.search reliably
    // from history.pushState, so tests use _setCurrentStudyUidForTests instead —
    // same code path as the runtime override a route watcher would use.
    _setCurrentStudyUidForTests('uid-a');
    setFindingsIconState('uid-a', 'colored');
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.className).toContain('lhrad-findings-button--colored');
  });
});


// ===========================================================================
// Button definition contract
// ===========================================================================
describe('findingsButtonDefinition', () => {
  it('references the showFindings command by id', () => {
    expect(findingsButtonDefinition.id).toBe(FINDINGS_BUTTON_ID);
    expect(findingsButtonDefinition.props.commands).toEqual([
      { commandName: 'lhrad.showFindings' },
    ]);
  });

  it('is always enabled — icon state signals presence, not clickability', () => {
    _resetFindingsIconStateCacheForTests();
    const result = findingsButtonDefinition.props.evaluate();
    expect(result.disabled).toBe(false);
  });
});
