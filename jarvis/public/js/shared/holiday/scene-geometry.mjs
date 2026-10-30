// Where the day scenes' art sits on its stage, as plain numbers: the Diwali toran and
// fireworks and its floor row, the floor rows of the others, how low the hero must sit while
// each of the nine scenes plays, and how far the worn item reaches above the mark. Always
// loaded (tiny, no drawing): the welcome page needs it before the art arrives, to keep the
// greeting and the mark clear of the art while a scene plays. The art (scenes/*.mjs) draws
// from these same numbers or is held to them by its tests, so the page and the drawing
// cannot drift apart.
//
// The stage is 800 units wide, x centred on 400 and y down from the top of the art layer;
// the page scales a unit to s = min(width / 800, 1.25) px. Every "units" in the scenes' code
// and comments means these.

// The toran: a thread sagging TORAN.sag units between hooks at y = TORAN.top, hung with
// blooms (radius TORAN.bloom) and mango leaves (TORAN.leaf tall). It drops in from above
// and overshoots TORAN.overshoot units before settling.
export const TORAN = { top: 4, sag: 72, leaf: 13, bloom: 5.4, overshoot: 4 };
export const SWAGS = 9; // blooms per swag are 1..SWAGS-1, at t = k / SWAGS

/** The thread's y at t (0..1) along one swag. */
export const swagY = (t) => TORAN.top + TORAN.sag * t * (1 - t);

export const TILE_K = 72 / 56; // units per tile unit of the mark, at any stage scale

export const GAP = 8; // room kept between the toran and what hangs below it, in units

// The fireworks are placed for a mark whose top sits at this y; they follow the mark
// wherever the page puts it.
export const MARK_TOP = 84;

// The three bursts. (x, y) is the flash centre for a mark at MARK_TOP; speed, gravity and ms drive
// the sparks (px per second, px per second squared, milliseconds). Sparks fly out at
// speed * (0.6 .. 1.1) in every direction.
export const SPARK_MAX = 1.1;
export const SPARK_R = 2.4;
export const BURSTS = [
  { n: 1, x: 250, y: 98, speed: 120, gravity: 70, ms: 1300 },
  { n: 2, x: 560, y: 80, speed: 120, gravity: 70, ms: 1300 },
  { n: 3, x: 680, y: 118, speed: 85, gravity: 150, ms: 2300 },
];

// How far below its thread the hanging at point k of a swag reaches: Diwali hangs a leaf at
// the odd points and a bloom at the even ones.
const marigoldReach = (k) => (k % 2 ? TORAN.leaf : TORAN.bloom);

/**
 * The lowest y a toran ever reaches: its deepest hanging, plus the settle overshoot.
 * `reach(k)` is how far the hanging at point k of a swag reaches below the thread (the
 * Diwali toran by default; Pongal's thoranam hangs other things).
 */
export function toranBottom(reach = marigoldReach) {
  let low = 0;
  for (let k = 1; k < SWAGS; k++)
    low = Math.max(low, swagY(k / SWAGS) + reach(k));
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
 * The least shift (units) of the fireworks from MARK_TOP: they hang off the mark, so a mark
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
export const HERO_FLOOR_GAP_PX = 16; // and between the hero's bottom (the sub-line) and the row's top
export const MIN_FLOOR_SCALE = 0.55; // below this the diyas stop being recognisable

/**
 * How far the part of the row that stands under the hero reaches above the baseline: the
 * row's `mid` when it says (null: nothing stands there, the tall props are at the ends), else
 * its whole `up`. Only this part has to stay clear of the hero's bottom.
 */
export const centreUp = (row) => (row.mid === undefined ? row.up : row.mid);

/**
 * How a floor row sits on a stage `hostH` px tall at stage scale s, with the baseline at
 * `ground` units. `row` is the row's extent, {up, rise, mid?}: how far its top stands above the
 * baseline, how far its bottom sits above it, and how far the part under the hero reaches (see
 * centreUp); the Diwali row by default, and every scene's art states its own. Two rules, in
 * this order:
 * - When the row's bottom would pass FLOOR_GAP_PX above the stage bottom (the welcome page
 *   scrolls on a short window), the bottom is pulled up to that line and the row shrinks by the
 *   same amount, as one group about its bottom centre, so its top stays where the layout put it.
 * - `clearTop` (units, y down; the hero's bottom plus HERO_FLOOR_GAP_PX): the part of the row
 *   under the hero may not reach above it, so the row shrinks further about its bottom.
 * Neither rule shrinks past MIN_FLOOR_SCALE. A stage with room gives scale 1. `bottom` is the
 * row's drawn bottom; `anchor` is where that bottom lands; `tight` says the part under the hero
 * still reaches above `clearTop` at the smallest scale (the scene then leaves that part out).
 */
export function floorFit({
  row = FLOOR_ROW,
  ground,
  hostH,
  s,
  gapPx = FLOOR_GAP_PX,
  clearTop,
}) {
  const bottom = ground - row.rise;
  const identity = {
    scale: 1,
    lift: 0,
    bottom,
    anchor: bottom,
    tight: false,
  };
  if (!Number.isFinite(bottom) || !(hostH > 0) || !(s > 0)) return identity;
  const over = pulledUp(bottom, hostH, s, gapPx);
  const height = row.up - row.rise;
  let scale = Math.max(MIN_FLOOR_SCALE, Math.min(1, 1 - over / height));
  const anchor = bottom - over;
  const mid = centreUp(row);
  let tight = false;
  if (Number.isFinite(clearTop) && Number.isFinite(mid) && mid > row.rise) {
    const room = (anchor - clearTop) / (mid - row.rise);
    scale = Math.max(MIN_FLOOR_SCALE, Math.min(scale, room));
    // Even the smallest row still reaches above the hero's clearance: the page can do no more.
    tight = anchor - scale * (mid - row.rise) < clearTop - TIGHT_SLACK_PX / s;
  }
  return { scale, lift: over, bottom, anchor, tight };
}

// How far (units) the row's bottom is pulled up to keep FLOOR_GAP_PX above the stage bottom.
const pulledUp = (bottom, hostH, s, gapPx) =>
  Math.max(0, bottom - (hostH - gapPx) / s);

// The page puts the hero on a whole pixel, which can be up to half a pixel lower than the place
// it worked out, so a row that is a pixel short of its gap still counts as fitting.
export const TIGHT_SLACK_PX = 1;

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

/** The baseline of the floor row in units: the bottom of the card band, less 2. */
export const groundOf = (cardsBottomPx, s) => cardsBottomPx / s - 2;

// The floor rows with something tall under the hero (the others stand at the stage's ends):
// `rise` is how far the row's bottom sits above its baseline and `reach` how far the part
// under the hero stands above it, in units, as the scene's art draws it (each scene's test
// holds its row to this table). The page needs them before the art arrives, to place the
// hero where that part stays clear of it (heroFloorTop).
export const FLOOR_UNDER_HERO = {
  diwali: { rise: FLOOR_ROW.rise, reach: FLOOR_ROW.up },
  onam: { rise: 0, reach: 90.85 },
  pongal: { rise: 0, reach: 142 },
};

/**
 * The y (px, down from the stage's top) the part of the floor row under the hero reaches up
 * to at the row's smallest scale, for `design`: the line the hero's bottom (and the gap
 * below it) must stay above. `cardsBottomPx` is where the card band ends. Undefined for a
 * design with nothing standing under the hero. Same fit as floorFit, so the page and the
 * scene agree.
 */
export function heroFloorTop({ design, s, hostH, cardsBottomPx }) {
  if (!Object.hasOwn(FLOOR_UNDER_HERO, design)) return undefined;
  const { rise, reach } = FLOOR_UNDER_HERO[design];
  const bottom = groundOf(cardsBottomPx, s) - rise;
  if (!Number.isFinite(bottom) || !(hostH > 0) || !(s > 0)) return undefined;
  const anchor = bottom - pulledUp(bottom, hostH, s, FLOOR_GAP_PX);
  return (anchor - MIN_FLOOR_SCALE * (reach - rise)) * s;
}

// ---- how low the hero must sit while each scene plays ---------------------------------------
// Every scene hangs something over the top of the stage (a toran, a sleigh, streamers...).
// The page keeps the mark's top below it, and needs the number before the art arrives, so the
// numbers live here, in units of the 800-unit stage (the page multiplies by its scale s).
// Each scene's own tests read its art and hold its number to this table (rounded up by under
// 0.01), so the table and the drawing cannot drift apart.

// The least y (stage units) the sky decorations of the day scenes may use: just under where
// the Diwali toran hangs, so the hero never has to sit lower for one scene than another.
export const CLEAR = toranBottom() + GAP;

/** The least y (px) the mark's top may have for a day scene whose top decoration needs `need` units. */
export const dayHeroTop = (s, need = 0, gap = GAP) => (CLEAR + need + gap) * s;

// A scene with no entry (Holi: splats round the edges, nothing over the hero) needs nothing.
export const HERO_TOP_UNITS = {
  diwali: 46.78,
  christmas: 95.9,
  newyear: 95.55,
  birthday: 82.78,
  eid: 54.78,
  pongal: 84.78,
  onam: 47.58,
  independence: 34,
};

/** The least y (px) the mark's top may have while `design`'s scene plays, at stage scale s. */
export const sceneHeroTop = (design, s) =>
  Object.hasOwn(HERO_TOP_UNITS, design) ? HERO_TOP_UNITS[design] * s : 0;

// ---- the worn item above the mark ------------------------------------------------------------
// The item the mark wears (hat, umbrella, crescent ...) is drawn on a layer over the mark and
// hangs past the tile's top. The page keeps all of it, plus WORN_GAP_PX, below the top of the
// stage: while the hero is centred, as it glides up and at rest. The real box is measured from
// the drawn layer when there is one (HolidayStage.vue); this table is what the page uses before
// the layer is mounted, and its floor afterwards. Units are the 56-unit tile space the worn
// art is drawn in: how far the item reaches above the tile's top, rounded up with a little
// room for the reactions (worn-overhang.test.mjs works the real reach out from the markup and
// the reaction keyframes and holds each number to it, so the table cannot drift from worn.mjs).
// A design with no entry wears nothing above the tile (its item sits on the tile or beside it).
export const WORN_OVERHANG = {
  christmas: 36,
  newyear: 22.5,
  eid: 19.5,
  onam: 19.5,
  holi: 19,
};

export const WORN_GAP_PX = 12; // room kept between the item's top and the stage's top

/** How far (tile units) the design's item reaches above the tile's top. */
export const wornOverhangUnits = (design) =>
  Object.hasOwn(WORN_OVERHANG, design) ? WORN_OVERHANG[design] : 0;
