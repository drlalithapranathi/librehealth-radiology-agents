/**
 * cxrTwoViewHangingProtocol — OHIF hanging protocol for a two-view chest X-ray.
 *
 * A CXR study routinely arrives as two series (PA and LAT), and OHIF's default
 * one-viewport-per-series layout stacks them, which is the wrong reading
 * gesture for chest — a radiologist wants PA and LAT side-by-side so pathology
 * on one can be corroborated on the other.
 *
 * This protocol matches CXR studies with 2+ series and lays them out as a
 * 2-column, 1-row grid: PA on the left (viewport 0), LAT on the right
 * (viewport 1). Assignment prefers ViewPosition to distinguish PA vs LAT; if
 * ViewPosition tags are absent (common with some modality workflows), it
 * falls back to positional assignment (first series -> left, second -> right)
 * so the display is at least side-by-side even if the specific-side call is
 * uncertain.
 *
 * Closes #73 item 4.
 *
 * Notes on the match rules:
 *   * `Modality: CR OR DX OR CX`: chest radiography lands under CR (computed
 *     radiography) or DX (digital radiography); the LibreHealth dev stack uses
 *     CX for CXR. Match all three so this works across setups.
 *   * `numberOfDisplaySets: {gte: 2}`: only fires on two-view; a single-view
 *     CXR falls back to the default 1-up display, which is correct for one PA.
 *
 * The protocol id `lhrad.cxr.two-view` is what OHIF stores in its protocol
 * registry; a mode config that wants to force this protocol can reference
 * it by that id.
 */

export const cxrTwoViewHangingProtocol = {
  id: 'lhrad.cxr.two-view',
  name: 'CXR (PA + LAT side-by-side)',
  createdDate: '2026-07-21',
  modifiedDate: '2026-07-21',

  // Higher score wins when multiple protocols match. Base OHIF protocols are
  // around 100–1000; we pick 1500 so this one wins over the default 1-up for
  // any CXR two-view study without needing to disable the defaults.
  protocolMatchingRules: [
    {
      id: 'cxr-modality',
      weight: 10,
      attribute: 'ModalitiesInStudy',
      constraint: { contains: ['CR', 'DX', 'CX'] },
      required: true,
    },
    {
      id: 'two-plus-series',
      weight: 5,
      attribute: 'numberOfDisplaySets',
      constraint: { greaterThan: 1 },
      required: true,
    },
  ],

  // Two viewports, 1 row × 2 columns. PA left, LAT right.
  stages: [
    {
      id: 'default',
      name: 'PA + LAT',
      viewportStructure: {
        type: 'grid',
        properties: {
          rows: 1,
          columns: 2,
        },
      },
      viewports: [
        // Viewport 0: LEFT — PA (posteroanterior).
        // No hardcoded VOI on either viewport: window/level comes from the
        // image (a fixed 3000/1500 window renders an 8-bit CR nearly black).
        {
          viewportOptions: {
            viewportId: 'lhrad-cxr-pa',
            toolGroupId: 'default',
            allowUnmatchedView: true,
          },
          displaySets: [{ id: 'paDisplaySet' }],
        },
        // Viewport 1: RIGHT — LAT (lateral).
        {
          viewportOptions: {
            viewportId: 'lhrad-cxr-lat',
            toolGroupId: 'default',
            allowUnmatchedView: true,
          },
          displaySets: [{ id: 'latDisplaySet' }],
        },
      ],
    },
  ],

  // Display-set selection rules. When ViewPosition tags are present, use them;
  // otherwise fall back to SeriesNumber order (first series -> PA slot, second
  // -> LAT slot). Two live-verified corrections to the first cut (#73 drill,
  // 2026-07-22): lateral chest views carry ViewPosition LL or RL far more often
  // than a literal LAT, and `displaySetIndex` is not an OHIF matching attribute
  // (its rule never fires), so the positional fallback rides SeriesNumber.
  displaySetSelectors: {
    paDisplaySet: {
      seriesMatchingRules: [
        {
          weight: 10,
          attribute: 'ViewPosition',
          constraint: { equals: 'PA' },
        },
        {
          weight: 8,
          attribute: 'ViewPosition',
          constraint: { equals: 'AP' },
        },
        // Fallback: first series in acquisition order.
        {
          weight: 1,
          attribute: 'SeriesNumber',
          constraint: { equals: 1 },
        },
      ],
    },
    latDisplaySet: {
      seriesMatchingRules: [
        {
          weight: 10,
          attribute: 'ViewPosition',
          constraint: { equals: 'LL' },
        },
        {
          weight: 10,
          attribute: 'ViewPosition',
          constraint: { equals: 'RL' },
        },
        {
          weight: 8,
          attribute: 'ViewPosition',
          constraint: { contains: 'LAT' },
        },
        // Fallback: second series in acquisition order.
        {
          weight: 1,
          attribute: 'SeriesNumber',
          constraint: { equals: 2 },
        },
      ],
    },
  },
};
