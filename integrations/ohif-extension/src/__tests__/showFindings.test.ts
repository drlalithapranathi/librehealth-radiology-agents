/**
 * Tests for showFindings command — the modal-launch shim that the toolbar button
 * invokes to display the AI Findings banner.
 *
 * These do not exercise the FindingsBannerPanel itself (it fetches, so testing it
 * requires DOM + fetch mocks; separate test file). This file's job is only to pin
 * the UIModalService invocation, the service-key probe (uiModalService vs
 * UIModalService), and the graceful fallback when the service is unavailable.
 */
import { describe, it, expect, vi } from 'vitest';

import { showFindings } from '../commands/showFindings';


// ===========================================================================
// Happy path: modal service picks up the call
// ===========================================================================
describe('showFindings: modal invocation', () => {
  it('calls uiModalService.show with the FindingsBannerPanel as content', () => {
    const show = vi.fn();
    showFindings({
      servicesManager: { services: { uiModalService: { show } } },
    });

    expect(show).toHaveBeenCalledTimes(1);
    const call = show.mock.calls[0][0];
    expect(call.title).toBe('AI Findings');
    // content is a React component reference (function or class); pin that it is set,
    // and that its display characteristics look like the panel (via truthy check —
    // JSDOM doesn't render, so we can't check the DOM output here).
    expect(call.content).toBeTruthy();
    expect(call.shouldCloseOnEsc).toBe(true);
    expect(call.shouldCloseOnOverlayClick).toBe(true);
  });

  it('probes the PascalCase UIModalService key too (older 3.6 minors)', () => {
    const show = vi.fn();
    showFindings({
      servicesManager: { services: { UIModalService: { show } } },
    });
    expect(show).toHaveBeenCalledTimes(1);
  });

  it('prefers camelCase over PascalCase when both are present', () => {
    // 3.5+ migration convention says camelCase wins. Pin so a future OHIF that
    // exposes both (during a transition) doesn't accidentally pick the wrong one.
    const camel = vi.fn();
    const pascal = vi.fn();
    showFindings({
      servicesManager: {
        services: { uiModalService: { show: camel }, UIModalService: { show: pascal } },
      },
    });
    expect(camel).toHaveBeenCalledTimes(1);
    expect(pascal).not.toHaveBeenCalled();
  });
});


// ===========================================================================
// Custom content override — for tests that want to assert the modal opened
// without pulling in the full FindingsBannerPanel component tree.
// ===========================================================================
describe('showFindings: content override', () => {
  it('passes the custom component through to the modal', () => {
    const show = vi.fn();
    const CustomContent = () => null;

    showFindings({
      servicesManager: { services: { uiModalService: { show } } },
      content: CustomContent,
    });

    expect(show.mock.calls[0][0].content).toBe(CustomContent);
  });
});


// ===========================================================================
// Fallback path: no modal service available
// ===========================================================================
describe('showFindings: fallback when modal service is missing', () => {
  it('does not throw when servicesManager is undefined', () => {
    expect(() => showFindings({})).not.toThrow();
  });

  it('does not throw when services is empty', () => {
    expect(() => showFindings({ servicesManager: { services: {} } })).not.toThrow();
  });

  it('invokes fallbackImpl when the modal service is absent', () => {
    const fallback = vi.fn();
    showFindings({ fallbackImpl: fallback });
    expect(fallback).toHaveBeenCalledTimes(1);
  });

  it('invokes fallbackImpl when modal.show throws', () => {
    const fallback = vi.fn();
    const show = vi.fn(() => {
      throw new Error('modal service internal error');
    });
    showFindings({
      servicesManager: { services: { uiModalService: { show } } },
      fallbackImpl: fallback,
    });
    expect(show).toHaveBeenCalledTimes(1);
    expect(fallback).toHaveBeenCalledTimes(1);
  });
});
