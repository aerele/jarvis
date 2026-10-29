// The drawing of the Independence Day (and Republic Day) scene: saffron morning wash, four
// kites, tricolour paper bits and the rosette that spins in. Coordinates are stage units
// (scene-geometry.mjs: 800 wide, x centred on 400); independence.mjs scales and places the
// group.
import { r2, rng } from "../stage.mjs";
import { GAP } from "../scene-geometry.mjs";
import { clearOfHero, edgeX } from "./scene-place.mjs";
import { boxRule, styleSVG } from "./scene-art.mjs";

// ---- geometry (plain numbers; the page needs them before the art loads) -------------------

// A kite is drawn 24 units wide, `up` units above its position to its top point and `down`
// below it to the end of its tail (including the sway).
export const KITE = { hw: 24, up: 28, down: 84 };

// The kites' places: x, and y either `dy` below the mark's top or `low` below the
// lowered line the two outer kites fly on in a taller stage. The highest hangs 2 units below
// the mark's top.
export const HIGH_KITE_DY = 2;
const SPOTS = [
  { x: 120, low: 0 },
  { x: 205, dy: 66 },
  { x: 590, dy: HIGH_KITE_DY },
  { x: 690, low: 24 },
];

/**
 * Where the four kites fly on this stage. geo: {from, to, mt, ground, keep, hu}. The outer
 * two fly lower on a taller stage. A kite is kept under the top of the stage and above the
 * floor, keeps its distance from its own edge of a wider stage, and is pushed sideways out of
 * the hero's way; a kite with no room is left out. Each is {i, x, y} (x, y: its centre point).
 */
export function kiteSpots(geo) {
  const high = geo.mt + 66;
  const low = high + 0.45 * (geo.ground - high - 60);
  const lo = KITE.up + GAP;
  const hi = Math.max(lo, geo.ground - KITE.down - GAP);
  return SPOTS.flatMap((spot, i) => {
    const y0 = "low" in spot ? low + spot.low : geo.mt + spot.dy;
    const y = Math.max(lo, Math.min(y0, hi));
    const box = {
      x: edgeX(spot.x, geo),
      y: y + (KITE.down - KITE.up) / 2,
      hw: KITE.hw,
      hh: (KITE.up + KITE.down) / 2,
    };
    const x = clearOfHero(box, geo.keep, geo.from, geo.to);
    return x === null ? [] : [{ i, x, y }];
  });
}

// ---- the drawing ------------------------------------------------------------------------------

export const independenceStyle = (p) =>
  styleSVG(
    p,
    `${boxRule(p, "50% 50%", "tb")}\n${boxRule(p, "50% 40%", "ksw")}
[data-theme="dark"] .${p}tbw{fill-opacity:.55;stroke-opacity:.55}`
  );

export const SKY_STOPS = [
  [0, "#fdba74", 0.42],
  [0.3, "#fed7aa", 0.22],
  [0.52, "#fff7ed", 0],
];

// The same wash in the dark theme: the pastels would go grey over the dark page, so it is
// the saffron of the day's accent at a lower alpha.
export const SKY_STOPS_DARK = [
  [0, "#ea7a17", 0.4],
  [0.3, "#f97316", 0.2],
  [0.52, "#f97316", 0],
];

export const defsSVG = (p) =>
  `<defs><clipPath id="${p}kc"><path d="M0 -16 L12 0 L0 18 L-12 0 Z"/></clipPath></defs>`;

const FILLS = [
  `<rect x="-12" y="-16" width="24" height="11.4" fill="#ff9933"/><rect x="-12" y="-4.6" width="24" height="11.3" fill="#ffffff"/><rect x="-12" y="6.7" width="24" height="11.3" fill="#138808"/>`,
  `<path d="M0 -16 L12 0 L0 18 Z" fill="#ef4444"/><path d="M0 -16 L-12 0 L0 18 Z" fill="#facc15"/>`,
  `<rect x="-12" y="-16" width="24" height="34" fill="#3b82f6"/><circle r="5" fill="#fff"/>`,
  `<path d="M-12 0 H12 L0 18 Z" fill="#16a34a"/><path d="M-12 0 H12 L0 -16 Z" fill="#f59e0b"/>`,
];
const TAILS = ["#ff9933", "#ef4444", "#3b82f6", "#16a34a"];

/** Kite number i (0 to 3), flying on a string that runs off the bottom of the stage `hu` tall. */
export const kiteSVG = (
  p,
  i,
  hu
) => `<g class="${p}kite" data-i="${i}" opacity="0">
    <path d="M0 29 Q${i < 2 ? -48 : 48} 220 ${i < 2 ? -112 : 112} ${r2(
  hu + 270
)}" stroke="#94a3b8" stroke-width=".8" fill="none"/>
    <g class="${p}ksw" data-i="${i}"><g transform="scale(1.6)">
      <path d="M0 18 q-6 8 0 16 q6 8 0 16" stroke="${
        TAILS[i]
      }" stroke-width="1.2" fill="none"/>
      <path d="M-3 28 L0 26 L3 28 L0 30 Z M-3 42 L0 40 L3 42 L0 44 Z" fill="${
        TAILS[i]
      }"/>
      <g clip-path="url(#${p}kc)">${FILLS[i]}</g>
      <path d="M0 -16 L12 0 L0 18 L-12 0 Z" fill="none" stroke="rgba(15,23,42,.28)" stroke-width=".8"/>
      <path d="M0 -16 V18 M-12 0 H12" stroke="rgba(15,23,42,.3)" stroke-width=".7"/>
    </g></g></g>`;

/** Thirty saffron, white and green paper bits above the top edge, over [from, to]. */
export function bitsSVG(p, from, to) {
  const rand = rng(91);
  return Array.from({ length: 30 }, (_, i) => {
    const white = i % 3 === 1;
    return `<rect class="${p}tb${white ? ` ${p}tbw` : ""}" x="${r2(
      from + rand() * (to - from)
    )}" y="${r2(-14 - rand() * 50)}" width="4" height="6" rx="1" fill="${
      ["#ff9933", "#ffffff", "#138808"][i % 3]
    }" stroke="${white ? "#cbd5e1" : "none"}" stroke-width=".6" opacity="0"/>`;
  }).join("");
}
