// The drawing of the Diwali scene: marigold toran, dusk sky, diyas and anar fountains,
// rockets and the three bursts. Coordinates are stage units (scene-geometry.mjs: 800 wide,
// x centred on 400); diwali.mjs scales and places the group.
import { burstSVG, lin, rad, rng, r2, ring } from "../stage.mjs";
import { BURSTS, DIYA, TORAN } from "../scene-geometry.mjs";
import { toranSVG as toran } from "./scene-art.mjs";

// Where the diwali scene puts the item's flame, in the 56-unit tile space of the mark.
export const FLAME_TILE = [60.5, 36];

export const LAND_MS = 4080; // the spark lights the flame; the item lands

// Every bright flash of the scene, from one table: the drawing below takes each radius from
// it, the animation in diwali.mjs takes each start and length from it, and the
// photosensitivity test reads it and checks the radii actually drawn. r is the flash
// radius in stage units; it grows to PEAK times that. At most 3 flashes in any second,
// none over 25% of the layer.
export const PEAK = 1.3;
export const FLASHES = [
  { cls: "flash1", at: 2450, ms: 750, r: 34 },
  { cls: "flash2", at: 3050, ms: 750, r: 34 },
  { cls: "flash3", at: 3900, ms: 750, r: 30 },
  { cls: "lit", at: LAND_MS, ms: 750, r: 16 },
];
const radiusOf = (cls) => FLASHES.find((f) => f.cls === cls).r;

const DIYA_FLAME = `<path d="M16.5 -20 C22 -13 23 -7.5 20.5 -4 C19 -2 14 -2 12.5 -4 C10.5 -7.5 12 -13 16.5 -20 Z" fill="url(#@dfl)"/>
  <path d="M16.5 -15 C19.8 -10.5 20.3 -7 18.8 -4.8 C17.8 -3.4 15.2 -3.4 14.2 -4.8 C12.7 -7 13.2 -10.5 16.5 -15 Z" fill="#fde047"/>
  <path d="M16.5 -10.5 C18.2 -8 18.3 -6 17.5 -4.8 C17 -4.1 16 -4.1 15.5 -4.8 C14.7 -6 14.8 -8 16.5 -10.5 Z" fill="#fffbeb"/>`;

/** Rules for the scene's own classes, so nothing leaks into the page. */
export const diwaliStyle = (p) => `<style>
.${p}fb{transform-box:fill-box;transform-origin:50% 50%}
.${p}fl{transform-box:fill-box;transform-origin:50% 100%}
.${p}flick{transform-box:fill-box;transform-origin:50% 100%;animation:${p}flick 1.9s ease-in-out infinite}
@keyframes ${p}flick{0%,100%{transform:scale(1,1) skewX(0);opacity:1}30%{transform:scale(.94,1.05) skewX(-3deg);opacity:.92}60%{transform:scale(1.04,.96) skewX(2deg);opacity:1}}
</style>`;

/** The sky wash (px space, full width). It stops above the greeting and fades at the sides. */
export const skySVG = (p, width, height) => `<defs>
  ${lin(
    `${p}sky`,
    [
      [0, "#1e1b4b", 0.88],
      [0.3, "#4c1d95", 0.55],
      [0.52, "#7c3aed", 0],
    ],
    0,
    1
  )}
  ${lin(
    `${p}skym`,
    [
      [0, "#fff", 0],
      [0.14, "#fff"],
      [0.86, "#fff"],
      [1, "#fff", 0],
    ],
    1,
    0
  )}
  <mask id="${p}skyk"><rect width="${r2(width)}" height="${r2(
  height
)}" fill="url(#${p}skym)"/></mask></defs>
  <rect class="${p}sky" width="${r2(width)}" height="${r2(
  height
)}" fill="url(#${p}sky)" mask="url(#${p}skyk)" opacity="0"/>`;

export const defsSVG = (p) => `<defs>
  ${rad(`${p}dg`, [
    [0, "#fde68a", 0.9],
    [0.5, "#fbbf24", 0.3],
    [1, "#fbbf24", 0],
  ])}
  ${lin(
    `${p}dclay`,
    [
      [0, "#f97316"],
      [0.55, "#c2410c"],
      [1, "#7c2d12"],
    ],
    0,
    1
  )}
  ${lin(
    `${p}dfl`,
    [
      [0, "#fbbf24"],
      [0.55, "#f97316"],
      [1, "#ea580c"],
    ],
    0,
    1
  )}
  ${lin(`${p}anar`, [
    [0, "#ef4444"],
    [1, "#991b1b"],
  ])}
  ${lin(
    `${p}rkt`,
    [
      [0, "#fde68a", 1],
      [1, "#fbbf24", 0],
    ],
    0,
    1
  )}
  ${rad(`${p}flash`, [
    [0, "#fff7ed", 0.95],
    [0.4, "#fde68a", 0.45],
    [1, "#fde68a", 0],
  ])}
</defs>`;

/** The marigold toran: swags of marigolds and mango leaves across units [from, to]. */
export const toranSVG = (p, from, to) =>
  toran(p, from, to, (x, y, n, k) => {
    const hue = Math.abs(n) % 2;
    const colour = Math.abs(n) % 4 === 1 ? "#15803d" : "#22c55e";
    const leaf = `<path d="M${x} ${y} q-3.2 ${TORAN.leaf / 2} 0 ${
      TORAN.leaf
    } q3.2 -6.2 0 -${TORAN.leaf} Z" fill="${colour}"/><path d="M${x} ${r2(
      y + 1
    )} V${r2(
      y + TORAN.leaf - 1.5
    )}" stroke="#14532d" stroke-opacity=".5" stroke-width=".6"/>`;
    const marigold = `<circle cx="${x}" cy="${y}" r="${TORAN.bloom}" fill="${
      hue ? "#facc15" : "#f97316"
    }"/>${ring(
      8,
      x,
      y,
      3.35,
      (cx, cy) =>
        `<circle cx="${cx}" cy="${cy}" r="1.62" fill="${
          hue ? "#fde047" : "#fb923c"
        }"/>`
    )}<circle cx="${x}" cy="${y}" r="1.62" fill="#c2410c"/>`;
    return (k % 2 ? leaf : "") + marigold;
  });

/**
 * The floor props: a row of lit diyas with an anar fountain at each end. `ground` is the
 * baseline. The scene's floor group (scene-runtime.mjs floorGroup) shrinks the whole row as
 * one group on a short stage, so the glow and the fountain sparks scale with the pots.
 */
export function floorSVG(p, ground, at = (x) => x) {
  const P = DIYA.pot;
  const rand = rng(13);
  const diya = (x) => `<g transform="translate(${x} ${
    ground - DIYA.lift
  }) scale(${r2(DIYA.scale)})">
    <circle class="${p}dglow" cx="16.5" cy="-10" r="22" fill="url(#${p}dg)" opacity="0"/>
    <ellipse cx="0" cy="14.5" rx="16" ry="2.4" fill="#000" opacity=".12"/>
    <path d="M-16 0 Q-15 12 0 13 Q11 13 15 3 L19 -1.5 Q13 .5 8 .5 Z" fill="url(#${p}dclay)"/>
    <path d="M-16 0 Q-4 -2.8 8 .5 Q-4 3 -16 0 Z" fill="#431407"/>
    <path d="M-13.5 5 Q0 9.5 13 4.2" stroke="#fde68a" stroke-width="1.2" fill="none"/>
    <path d="M16.5 -3.5 V-1" stroke="#451a03" stroke-width="1.2" stroke-linecap="round"/>
    <g class="${p}dflame ${p}fl"><g class="${p}flick">${DIYA_FLAME.replaceAll(
    "@",
    p
  )}</g></g></g>`;
  const anar = (x) => `<g transform="translate(${x} ${ground - 8}) scale(${P})">
    <g>${Array.from(
      { length: 72 },
      (_, i) =>
        `<circle class="${p}fount" r="${r2(
          1.1 + rand() * 1.5
        )}" cy="-16" fill="${
          ["#facc15", "#f59e0b", "#ea580c", "#e11d48"][i % 4]
        }" opacity="0"/>`
    ).join("")}</g>
    <ellipse cx="0" cy="1" rx="10" ry="2.4" fill="#000" opacity=".12"/>
    <path d="M-8 0 L8 0 L4.5 -16 L-4.5 -16 Z" fill="url(#${p}anar)"/>
    <path d="M-6.6 -5 H6.6 M-5.4 -10.5 H5.4" stroke="#fde68a" stroke-width="1.3"/>
    <ellipse cx="0" cy="-16" rx="4.5" ry="1.4" fill="#7f1d1d"/></g>`;
  const xs = [90, 193, 297, 400, 503, 607, 710];
  return `${xs.map((x) => diya(at(x))).join("")}${anar(at(245))}${anar(
    at(555)
  )}`;
}

/**
 * Rockets and the three bursts. `dy` is the mark's y shift from MARK_TOP, `xs` where each burst
 * stands across the stage (the phone squeezes them into its window).
 */
export function skyShowSVG(p, dy, xs = BURSTS.map((b) => b.x)) {
  const flash = (n, x, y) =>
    `<g transform="translate(${x} ${y})"><circle class="${p}flash${n} ${p}fb" r="${radiusOf(
      `flash${n}`
    )}" fill="url(#${p}flash)" opacity="0"/>`;
  const rocket = (n) =>
    `<g class="${p}rk${n}" opacity="0"><path d="M0 0 L0 18" stroke="url(#${p}rkt)" stroke-width="2.2" stroke-linecap="round"/><circle r="2.3" fill="#fff7ed"/></g>`;
  return `${flash(1, xs[0], BURSTS[0].y + dy)}${burstSVG(
    `${p}b1`,
    40,
    ["#f59e0b", "#ea580c", "#facc15", "#e11d48"],
    3
  )}</g>
    ${flash(2, xs[1], BURSTS[1].y + dy)}${burstSVG(
    `${p}b2`,
    40,
    ["#db2777", "#9333ea", "#ec4899", "#f59e0b"],
    6
  )}</g>
    ${flash(3, xs[2], BURSTS[2].y + dy)}${burstSVG(
    `${p}b3`,
    32,
    ["#fde047", "#fbbf24", "#fff7ed"],
    9
  )}</g>
    ${rocket(1)}${rocket(2)}${rocket(3)}`;
}

/** The spark that drifts down and lights the item's flame, and the glow where it lands. */
export const lightSVG = (
  p,
  x,
  y
) => `<g class="${p}carry" opacity="0"><circle r="7" fill="#fde047" opacity=".35"/><circle r="2.6" fill="#fffbeb"/></g>
  <g transform="translate(${r2(x)} ${r2(
  y
)})"><circle class="${p}lit ${p}fb" r="${radiusOf(
  "lit"
)}" fill="url(#${p}flash)" opacity="0"/></g>`;
