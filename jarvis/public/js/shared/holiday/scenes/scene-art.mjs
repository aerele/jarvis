// Drawing helpers every day scene shares (the scenes' own art lives in <design>-art.mjs).
// Coordinates are stage units (scene-geometry.mjs) unless a function says px. Lazy, like the
// scenes.
import { lin, r2, STAR } from "../stage.mjs";
import { SWAGS, TORAN, swagY } from "../scene-geometry.mjs";
import { wornMarkup } from "../worn.mjs";

/** CSS box rule: these classes transform about `origin` of their own box. */
export const boxRule = (p, origin, ...names) =>
  `${names
    .map((n) => `.${p}${n}`)
    .join(",")}{transform-box:fill-box;transform-origin:${origin}}`;

/** Rules for the scene's own classes, so nothing leaks into the page. `rules` adds more. */
export const styleSVG = (p, rules = "") => `<style>
${boxRule(p, "50% 50%", "fb", "sp", "drop")}
.${p}flick{transform-box:fill-box;transform-origin:50% 100%;animation:${p}flick 1.9s ease-in-out infinite}
@keyframes ${p}flick{0%,100%{transform:scale(1,1) skewX(0);opacity:1}30%{transform:scale(.94,1.05) skewX(-3deg);opacity:.92}60%{transform:scale(1.04,.96) skewX(2deg);opacity:1}}
${rules}
</style>`;

/**
 * The sky wash (px space, full width). It stops above the greeting and fades at the sides.
 * `dark` is the dark theme's own stops, when the light ones would go grey over the dark page:
 * pastel colours at a low alpha are drawn over the page, so a dark page needs saturated
 * colours instead. The dark theme (html[data-theme="dark"]) then fills the sky from them.
 */
export const skyWashSVG = (p, stops, width, height, dark) => `<defs>
  ${lin(`${p}sky`, stops, 0, 1)}${
  dark
    ? `
  ${lin(`${p}skyd`, dark, 0, 1)}
  <style>[data-theme="dark"] .${p}sky{fill:url(#${p}skyd)}</style>`
    : ""
}
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

/** A diya-style flame (three nested tongues) filled from the gradient `grad`. */
export const flameSVG = (
  grad
) => `<path d="M16.5 -20 C22 -13 23 -7.5 20.5 -4 C19 -2 14 -2 12.5 -4 C10.5 -7.5 12 -13 16.5 -20 Z" fill="url(#${grad})"/>
  <path d="M16.5 -15 C19.8 -10.5 20.3 -7 18.8 -4.8 C17.8 -3.4 15.2 -3.4 14.2 -4.8 C12.7 -7 13.2 -10.5 16.5 -15 Z" fill="#fde047"/>
  <path d="M16.5 -10.5 C18.2 -8 18.3 -6 17.5 -4.8 C17 -4.1 16 -4.1 15.5 -4.8 C14.7 -6 14.8 -8 16.5 -10.5 Z" fill="#fffbeb"/>`;

/** A four-point sparkle at (x, y), scale k. Hidden until animated. */
export const sparkleSVG = (p, x, y, k, colour) =>
  `<g class="${p}sp" opacity="0"><path d="${STAR}" fill="${colour}" transform="translate(${r2(
    x
  )} ${r2(y)}) scale(${r2(k)}) translate(-12 -12)"/></g>`;

// The land sparkles: nine gold stars that burst `spread` units outward over `ms`. `r` is the
// bound of the whole burst (spread plus half a star at full size), for the photosensitivity
// check. One burst in a scene, far below 25% of the layer.
export const SPARKLE = { n: 9, spread: 22, ms: 650, r: 32 };

/** The sparkles, drawn at the origin of the pin that holds them. */
export const landSparklesSVG = (p) =>
  `<g>${Array.from(
    { length: SPARKLE.n },
    (_, i) =>
      `<g class="${p}sp" opacity="0"><path d="${STAR}" fill="${
        i % 2 ? "#fbbf24" : "#fde047"
      }" transform="scale(.3) translate(-12 -12)"/></g>`
  ).join("")}</g>`;

/**
 * A copy of the worn item to fly over the page, drawn in the mark's 56-unit tile space and
 * hidden until animated (Scene.item pins it to the mark). `options` is wornMarkup's:
 * the copy has no contact shadow, which belongs on the tile.
 */
export const itemSVG = (p, design, options = {}) =>
  `<g class="${p}drop" opacity="0">${wornMarkup(design, {
    idPrefix: `${p}it`,
    motion: false,
    noShadow: true,
    ...options,
  })}</g>`;

/**
 * A floor row drawn at its own y (the art's, not the page's): it stands on `base` at group scale `g`; `top` is its
 * tallest point and `bottom` its lowest (drawn y). Adds the extent floorFit takes: `up` is how
 * far its top stands above the baseline, `rise` how far its bottom sits above it. `core` is the
 * drawn y of the tallest part that stands under the hero's column (the middle of the stage):
 * that part keeps clear of the hero's bottom (floorFit's clearTop). Omit it when the whole row
 * counts, pass null when the tall props are all at the ends and nothing stands in the middle.
 */
export const floorRow = ({ base, g = 1, top, bottom = base, core }) => ({
  base,
  g,
  top,
  bottom,
  up: (base - top) * g,
  rise: Math.max(0, (base - bottom) * g),
  ...(core === undefined
    ? {}
    : { mid: core === null ? null : (base - core) * g }),
});

/**
 * A toran across the units [from, to]: a thread sagging TORAN.sag between hooks every 200
 * units, with `hang(x, y, n, k)` markup at each of the SWAGS - 1 points along every swag
 * (`n` counts the points from the stage's left, `k` is the point within its swag). Starts
 * hidden; Scene.dropToran lowers it in.
 */
export function toranSVG(p, from, to, hang) {
  const first = Math.floor(from / 200);
  const last = Math.ceil(to / 200);
  let d = `M${first * 200} ${TORAN.top}`;
  let items = "";
  for (let i = first; i < last; i++) {
    d += ` Q${i * 200 + 100} ${TORAN.top + TORAN.sag / 2} ${i * 200 + 200} ${
      TORAN.top
    }`;
    for (let k = 1; k < SWAGS; k++) {
      const t = k / SWAGS;
      items += hang(r2(i * 200 + 200 * t), r2(swagY(t)), i * 8 + k, k);
    }
  }
  return `<g class="${p}toran" opacity="0"><path d="${d}" stroke="#a16207" stroke-width="1.2" fill="none"/>${items}</g>`;
}
