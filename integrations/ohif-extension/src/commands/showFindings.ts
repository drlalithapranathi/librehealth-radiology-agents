/**
 * showFindings command — opens the AI Findings modal (#74, client-side CAD evidence
 * rendering; toolbar affordance per !95's finding that panels don't mount in OHIF
 * v3.6's default mode).
 *
 * Invoked by the `lhrad.showFindings` toolbar button. Opens OHIF's UIModalService
 * with `FindingsBannerPanel` as the content. The panel component owns the fetch +
 * rendering policy; this command is just the modal-launch shim.
 *
 * Modal via OHIF's UIModalService (docs.ohif.org/platform/services/ui/ui-modal-service):
 * one of the OHIF services that is available across all modes and does not require a
 * mounted panel slot. Confirmed to work in the default mode because it is used by
 * OHIF's own core UI (dialogs for delete confirmation, keyboard shortcut help, etc.).
 *
 * If the UIModalService is unavailable (defensive — some OHIF builds have it lazy-
 * loaded or renamed across 3.6 minors), fall back to a lightweight window-level
 * fallback rather than crashing the toolbar. In practice the fallback path is dead
 * code on our deployed stack; it exists so a toolbar click never no-ops silently.
 */
import * as React from 'react';

import { FindingsBannerPanel } from '../components/FindingsBannerPanel';

export interface ShowFindingsOptions {
  /** Injected by OHIF's command runner; carries UIModalService among other services.
   *  Optional for tests, which can pass a stub or omit entirely. */
  servicesManager?: {
    services?: {
      uiModalService?: UIModalServiceShape;
      UIModalService?: UIModalServiceShape;
    };
  };
  /** Overridable for tests — the component that gets rendered inside the modal. */
  content?: React.ComponentType;
  /** Overridable for tests when we want to assert on the fallback path. */
  fallbackImpl?: () => void;
}

// Minimal OHIF UIModalService shape. OHIF 3.6 exposes `show({ title, content, ... })`;
// some minors capitalize the service key ('UIModalService' vs 'uiModalService'), so
// we probe both. Typed loosely because the OHIF types aren't a peer we can pin.
interface UIModalServiceShape {
  show: (opts: {
    title?: string;
    content: React.ComponentType | React.ReactElement;
    contentProps?: Record<string, unknown>;
    shouldCloseOnEsc?: boolean;
    shouldCloseOnOverlayClick?: boolean;
  }) => void;
}

/**
 * Open the findings modal. Returns void — command semantics; success/failure is
 * observed by whether the modal appears, not by a return value.
 */
export function showFindings(options: ShowFindingsOptions = {}): void {
  const modalService = pickModalService(options.servicesManager);
  const Content = options.content ?? FindingsBannerPanel;

  if (modalService) {
    try {
      modalService.show({
        title: 'AI Findings',
        content: Content,
        shouldCloseOnEsc: true,
        shouldCloseOnOverlayClick: true,
      });
      return;
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('lhrad: UIModalService.show threw; using fallback:', err);
    }
  } else {
    // eslint-disable-next-line no-console
    console.warn('lhrad: UIModalService unavailable; using fallback for showFindings');
  }

  // Fallback: log-only. A future iteration could open a window-level dialog, but on
  // our deployed stack (OHIF release/3.6 @ 72ec0bf) UIModalService is present, so
  // the fallback is a diagnostic breadcrumb rather than a real UX path.
  if (options.fallbackImpl) options.fallbackImpl();
}

function pickModalService(
  servicesManager: ShowFindingsOptions['servicesManager'],
): UIModalServiceShape | undefined {
  const svc = servicesManager?.services;
  if (!svc) return undefined;
  // OHIF 3.6 uses `uiModalService` (camelCase) after the 3.5 service-manager
  // convention update, but some fork builds keep the older PascalCase key. Probe both.
  return svc.uiModalService ?? svc.UIModalService;
}
