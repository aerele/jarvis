// Where the Diwali art sits on its stage, as plain numbers. Always loaded (tiny, no
// drawing): the welcome page needs it before the art arrives, to keep the greeting and the
// mark clear of the toran and the fireworks while the scene plays. The art (scenes/
// diwali-art.mjs, scenes/diwali.mjs) draws from these same numbers, so the page and the
// drawing cannot drift apart.
//
// Units are the mock's 800-unit stage: x centred on 400, y down from the top of the art
// layer; the page scales them by s = min(width / 800, 1.25).

// The toran: a thread sagging TORAN.sag units between hooks at y = TORAN.top, hung with
// blooms (radius TORAN.bloom) and mango leaves (TORAN.leaf tall). It drops in from above
// and overshoots TORAN.overshoot units before settling.
export const TORAN = { top: 4, sag: 72, leaf: 13, bloom: 5.4, overshoot: 4 };
export const SWAGS = 9; // blooms per swag are 1..SWAGS-1, at t = k / SWAGS

/** The thread's y at t (0..1) along one swag. */
export const swagY = (t) => TORAN.top + TORAN.sag * t * (1 - t);

export const GAP = 8; // room kept between the toran and what hangs below it, in units

// In the mock the mark's top sits at this y; the fireworks are placed relative to it, so
// they follow the mark wherever the page puts it.
export const MARK_TOP = 84;

// The three bursts. (x, y) is the flash centre in the mock; speed, gravity and ms drive
// the sparks (px per second, px per second squared, milliseconds). Sparks fly out at
// speed * (0.6 .. 1.1) in every direction.
export const SPARK_MAX = 1.1;
export const SPARK_R = 2.4;
export const BURSTS = [
  { n: 1, x: 250, y: 98, speed: 120, gravity: 70, ms: 1300 },
  { n: 2, x: 560, y: 80, speed: 120, gravity: 70, ms: 1300 },
  { n: 3, x: 680, y: 118, speed: 85, gravity: 150, ms: 2300 },
];

/** The lowest y the toran ever reaches: the deepest leaf, plus the settle overshoot. */
export function toranBottom() {
  let low = 0;
  for (let k = 1; k < SWAGS; k++) {
    const y = swagY(k / SWAGS);
    low = Math.max(low, y + (k % 2 ? TORAN.leaf : TORAN.bloom));
  }
  return low + TORAN.overshoot;
}

/** How far a burst's highest spark gets above its flash centre. */
export function burstRise(b) {
  const v = b.speed * SPARK_MAX;
  const t = Math.min(v / b.gravity, b.ms / 1000);
  return v * t - 0.5 * b.gravity * t * t + SPARK_R;
}

/** The highest y any burst reaches, as units above the mark's top (positive is up). */
export function fireworksAboveMark() {
  return Math.max(...BURSTS.map((b) => MARK_TOP - b.y + burstRise(b)));
}

/**
 * The least y (px) the hero's top (the mark's top) may have while the scene plays, at stage
 * scale s: just below the toran. `gap` is breathing room in units.
 */
export function heroTopMin(s, gap = GAP) {
  return (toranBottom() + gap) * s;
}

/**
 * The least shift (units) of the fireworks from the mock: they hang off the mark, so a mark
 * near the top would send the highest burst up into the toran. The scene never lets the
 * fireworks sit higher than this, whatever the page does with the mark.
 */
export function fireworksMinDy(gap = GAP) {
  return toranBottom() + gap + fireworksAboveMark() - MARK_TOP;
}

// ---- the floor row: diyas and flowerpots (anar) along the bottom ------------------------
// The diya as drawn (scenes/diwali-art.mjs takes these numbers from here): its origin sits
// `lift` units above the baseline, at `scale` (the row's pot scale times the diya's own).
// glowTop and shadowBottom are the glow's top edge and the shadow's bottom edge in the
// diya's own units, the two ends of the row.
export const DIYA = {
  lift: 20,
  pot: 1.35,
  own: 0.85,
  scale: 1.35 * 0.85,
  glowTop: -32,
  shadowBottom: 16.9,
};

/** The row's extent in units: `up` from the baseline to the glow top, `rise` the shadow's bottom above it. */
export const FLOOR_ROW = {
  up: DIYA.lift - DIYA.glowTop * DIYA.scale,
  rise: DIYA.lift - DIYA.shadowBottom * DIYA.scale,
};

export const FLOOR_GAP_PX = 12; // room kept between the row's bottom and the stage's bottom
export const MIN_FLOOR_SCALE = 0.55; // below this the diyas stop being recognisable

/**
 * How the floor row sits on a stage `hostH` px tall at stage scale `s`, with the baseline at
 * `ground` units. When the row's bottom would pass FLOOR_GAP_PX above the stage bottom (the
 * welcome page scrolls on a short window), the bottom is pulled up to that line and the row
 * shrinks by the same amount, as one group about its bottom centre, so its top stays where
 * the layout put it; it never shrinks past MIN_FLOOR_SCALE. A stage with room gives scale 1.
 * `bottom` is the row's drawn bottom; `anchor` is where that bottom lands.
 */
export function floorFit({ ground, hostH, s, gapPx = FLOOR_GAP_PX }) {
  const bottom = ground - FLOOR_ROW.rise;
  const identity = { scale: 1, lift: 0, bottom, anchor: bottom };
  if (!Number.isFinite(bottom) || !(hostH > 0) || !(s > 0)) return identity;
  const over = Math.max(0, bottom - (hostH - gapPx) / s);
  const height = FLOOR_ROW.up - FLOOR_ROW.rise;
  const scale = Math.max(MIN_FLOOR_SCALE, Math.min(1, 1 - over / height));
  return { scale, lift: over, bottom, anchor: bottom - over };
}

/** Where a point of the drawn row lands once the row is fitted (the row scales about x = 400). */
export function fitPoint(fit, [x, y]) {
  return [
    400 + fit.scale * (x - 400),
    fit.anchor + fit.scale * (y - fit.bottom),
  ];
}

/** The SVG transform that fits the floor row group. */
export const floorTransform = (fit) =>
  `translate(400 ${round(fit.anchor)}) scale(${round(
    fit.scale
  )}) translate(-400 ${round(-fit.bottom)})`;

const round = (n) => Math.round(n * 100) / 100;
