// The drawing of the New Year scene: four firework bursts, the coming year in glowing gold
// numbers above Jarvis, and two confetti cannons. Coordinates are stage units
// (scene-geometry.mjs: 800 wide, x centred on 400); newyear.mjs scales and places the
// group. The year is text drawn from the `year` option, never part of the art.
import { burstSVG, lin, rad, r2 } from "../stage.mjs";
import { styleSVG } from "./scene-art.mjs";
import {
  CLEAR,
  MARK_TOP,
  TILE_K,
  WORN_OVERHANG,
  burstRise,
} from "../scene-geometry.mjs";

// Every bright flash of the scene, from one table (as diwali-art.mjs does): the drawing takes
// each radius from it, the animation takes each start and length from it, and the
// photosensitivity test reads it and checks the radii actually drawn. r is the flash radius
// in stage units; it grows to PEAK times that. At most 3 flashes in any second, none over
// 25% of the layer.
export const PEAK = 1.3;
export const ROCKET_MS = 600;
const FIRST = 250;
const EVERY = 480;
export const BURSTS = [
  { x: 150, y: 110, colours: ["#f59e0b", "#ea580c", "#e11d48"] },
  { x: 290, y: 64, colours: ["#db2777", "#9333ea", "#f59e0b"] },
  { x: 520, y: 60, colours: ["#0284c7", "#0891b2", "#f59e0b"] },
  { x: 660, y: 108, colours: ["#16a34a", "#e11d48", "#9333ea"] },
].map((b, i) => ({
  ...b,
  n: i,
  sparks: 36,
  seed: 60 + i,
  speed: 110,
  gravity: 70,
  ms: 1300,
}));
export const FLASHES = BURSTS.map((b) => ({
  cls: `nflash${b.n}`,
  at: FIRST + b.n * EVERY + ROCKET_MS,
  ms: 750,
  r: 34,
}));
const radiusOf = (cls) => FLASHES.find((f) => f.cls === cls).r;
export const rocketAt = (n) => FIRST + n * EVERY;

/**
 * The least shift (units) of the fireworks from MARK_TOP: they hang off the mark, so a mark
 * near the top would send the highest burst up past the clearance line.
 */
export const FIREWORKS_MIN_SHIFT =
  CLEAR +
  Math.max(...BURSTS.map((b) => MARK_TOP - b.y + burstRise(b))) -
  MARK_TOP;

// The year: digits stand about 0.72 of the font size, with a little for the glow stroke. Its
// baseline stands `above` units over the mark's top (more when the party hat reaches higher:
// see yearAbove), so the year sits just over the hat whatever the stage's height.
export const YEAR = { maxPx: 46, minPx: 22, cap: 0.72, pad: 2, above: 30 };

/**
 * How far (units) the year's baseline stands above the mark's top: YEAR.above, and never
 * less than clear of the party hat, which reaches WORN_OVERHANG.newyear tile units (`k`
 * units each) above the tile.
 */
export const yearAbove = (k) =>
  Math.max(YEAR.above, WORN_OVERHANG.newyear * k + YEAR.pad);

/** The least room (units) between the clearance line and the mark's top the year needs. */
export const yearRoom = (k = TILE_K) =>
  YEAR.pad + YEAR.minPx * YEAR.cap + yearAbove(k);

/**
 * Font size and baseline of the year: just above the hat, as big as the room under the
 * clearance line allows. `mt` is the mark's top, `k` the units per tile unit of the mark.
 * Short of room even at the smallest size, the baseline stops at the clearance line.
 */
export function yearPlan({ airTop, mt, k }) {
  const base = mt - yearAbove(k);
  const px = Math.max(
    YEAR.minPx,
    Math.min(YEAR.maxPx, (base - airTop - YEAR.pad) / YEAR.cap)
  );
  const lowest = airTop + YEAR.pad + px * YEAR.cap;
  return { px: r2(px), baseline: r2(Math.max(base, lowest)) };
}

/** The year as the four digits drawn, or "" when the page gave none that make sense. */
export const yearText = (year) =>
  /^\d{4}$/.test(String(year ?? "")) ? String(year) : "";

/** The scene's style element: rules for its own classes, so nothing leaks into the page. */
export const newyearStyle = (p) =>
  styleSVG(
    p,
    `.${p}yrwrap{transform-box:fill-box;transform-origin:50% 50%}
.${p}yr{font-weight:800;letter-spacing:.04em;fill:url(#${p}yrG);stroke:#f59e0b;stroke-width:1;paint-order:stroke;filter:drop-shadow(0 0 6px rgba(251,191,36,.75))}`
  );

export const defsSVG = (p) => `<defs>
  ${rad(`${p}nflash`, [
    [0, "#fff7ed", 0.9],
    [0.4, "#fde68a", 0.35],
    [1, "#fde68a", 0],
  ])}
  ${lin(
    `${p}nrkt`,
    [
      [0, "#fde68a", 1],
      [1, "#fbbf24", 0],
    ],
    0,
    1
  )}
  ${lin(`${p}can`, [
    [0, "#a855f7"],
    [1, "#6d28d9"],
  ])}
  ${lin(
    `${p}yrG`,
    [
      [0, "#fffbeb"],
      [0.5, "#fde047"],
      [1, "#f59e0b"],
    ],
    0,
    1
  )}
</defs>`;

/** The four bursts and their rockets. `shift` moves them down from the heights they are listed at. */
export function skySVG(p, shift) {
  const burst = (b) =>
    `<g transform="translate(${b.x} ${r2(
      b.y + shift
    )})"><circle class="${p}nflash${b.n} ${p}fb" r="${radiusOf(
      `nflash${b.n}`
    )}" fill="url(#${p}nflash)" opacity="0"/>${burstSVG(
      `${p}nb${b.n}`,
      b.sparks,
      b.colours,
      b.seed
    )}</g>`;
  const rocket = (b) =>
    `<g class="${p}nrk${b.n}" opacity="0"><path d="M0 0 L0 18" stroke="url(#${p}nrkt)" stroke-width="2.2" stroke-linecap="round"/><circle r="2.3" fill="#fff7ed"/></g>`;
  return `${BURSTS.map(burst).join("")}${BURSTS.map(rocket).join("")}`;
}

/** The year in glowing gold numbers, centred on the mark. */
export const yearSVG = (p, text, plan) =>
  text
    ? `<g class="${p}yrwrap" opacity="0"><text class="${p}yr" x="400" y="${plan.baseline}" font-size="${plan.px}" text-anchor="middle">${text}</text></g>`
    : "";

// ---- the cannons -----------------------------------------------------------------------

export const CANNON = { scale: 1.55, lift: 8, tilt: 38, x: [70, 730] };
// The floor row: a cannon at each end, standing on the baseline; the muzzle is where the
// confetti and the party hat leave from.
// Nothing stands under the hero (mid: null).
export const FLOOR_ROW = { up: 52, rise: 0, mid: null };

/** Where a cannon's muzzle is, relative to the baseline: `dir` is 1 for the left cannon. */
export function muzzle(x, dir) {
  const P = CANNON.scale;
  return [r2(x + dir * 17.9 * P), r2(-CANNON.lift - 22.9 * P)];
}

const CONFETTI = [
  "#f472b6",
  "#facc15",
  "#38bdf8",
  "#4ade80",
  "#a855f7",
  "#fb923c",
];

function cannonSVG(p, ground, x, flip) {
  const P = CANNON.scale;
  return `<g transform="translate(${x} ${r2(ground - CANNON.lift)}) scale(${
    flip ? -P : P
  } ${P})"><g transform="rotate(${CANNON.tilt})">
    <ellipse cx="0" cy="3" rx="11" ry="2.6" fill="#000" opacity=".12"/>
    <rect x="-6" y="-26" width="12" height="28" rx="3" fill="url(#${p}can)"/>
    <path d="M-6 -18 H6 M-6 -9 H6" stroke="#fde047" stroke-width="2"/>
    <rect x="-7.5" y="-29" width="15" height="4.5" rx="2" fill="#fbbf24"/></g></g>`;
}

const confetti = (cls) =>
  Array.from(
    { length: 44 },
    (_, i) =>
      `<rect class="${cls}" x="-1.6" y="-3.4" width="3.2" height="6.8" rx="1" fill="${
        CONFETTI[i % 6]
      }" opacity="0"/>`
  ).join("");

/** The floor row's drawing: two confetti cannons, standing on `ground`. */
export function floorSVG(p, ground) {
  const [left, right] = CANNON.x;
  const at = ([mx, my]) => `translate(${mx} ${r2(ground + my)})`;
  return `<g transform="${at(muzzle(left, 1))}">${confetti(
    `${p}cl`
  )}</g><g transform="${at(muzzle(right, -1))}">${confetti(
    `${p}cr`
  )}</g>${cannonSVG(p, ground, left, false)}${cannonSVG(
    p,
    ground,
    right,
    true
  )}`;
}
