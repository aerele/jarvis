// Where a scene's pieces go on the page: kept clear of the hero, kept at their own edge of
// the stage. Plain functions on the numbers Scene.measure gives (units of the 800-unit stage,
// x centred on 400; px for the boxes the page reports).
import { GAP } from "../scene-geometry.mjs";

const SUBLINE_PX = 420; // the sub-line is wider than the heading; keep clear of it too
export const BELOW_HEADING_PX = 44; // and of the sub-line's height

/** A point in stage units -> the mark's 56-unit tile units (geo as Scene.measure gives it). */
export const toTile = (geo, [x, y]) => [
  (x - 400) / geo.k + 28,
  (y - geo.mt) / geo.k,
];

/**
 * The hero (mark, heading, sub-line) as a box in units, with GAP of room around it. The page
 * gives the hero's own box (b.hero); without it the sub-line is guessed from the heading.
 */
export function heroKeepOut(b, s) {
  const { hero } = b;
  const half =
    Math.max(b.heading.w, b.mark.w, hero ? hero.w : SUBLINE_PX) / 2 / s;
  const bottom = hero
    ? hero.y + hero.h
    : b.heading.y + b.heading.h + BELOW_HEADING_PX;
  return {
    l: 400 - half - GAP,
    r: 400 + half + GAP,
    t: b.mark.y / s - GAP,
    b: bottom / s + GAP,
  };
}

/**
 * Moves a piece sideways, away from the hero, when it would overlap it. `box` is the piece's
 * centre and half sizes in units. Returns its x, or null when there is no room left on its
 * side of the stage (the piece is then left out).
 */
export function clearOfHero({ x, y, hw, hh }, keep, from, to) {
  const hits =
    x + hw > keep.l && x - hw < keep.r && y + hh > keep.t && y - hh < keep.b;
  if (!hits) return x;
  const nx = x < 400 ? keep.l - hw : keep.r + hw;
  return nx - hw >= from && nx + hw <= to ? nx : null;
}

// How much narrower than the 800-unit stage the window [from, to] is, as a factor (1: not
// narrower). Only the phone window is: a host never shows less than 800 units on desktop
// (to - from = width / s, with s at most width / 800), so the tolerance only absorbs float
// noise there and keeps every desktop position exactly as it was.
export const squeezeOf = (from, to) => {
  const k = (to - from) / 800;
  return k >= 1 - 1e-9 ? 1 : k;
};

/**
 * An x placed on the 800-unit stage, squeezed about the mark's column (400) into a narrower
 * window (the phone's); exactly `x` when the window is not narrower. For props at a fixed x
 * that is not an edge (the floor row's diyas, the moon, the fireworks, ...).
 */
export const squeezeX = (x, geo) =>
  geo.squeeze < 1 ? 400 + (x - 400) * geo.squeeze : x;

/**
 * An x on the 800-unit stage for a prop that belongs to an edge. A wider stage: the same
 * distance from its own edge. A narrower one (the phone): squeezed with the whole stage, so
 * an edge prop stays at its edge and none crosses the centre. (Keeping the distance on a
 * 540-unit window would put a prop 150 units in from the edge at x = 280, over the hero.)
 */
export const edgeX = (x, geo) =>
  geo.squeeze < 1
    ? squeezeX(x, geo)
    : x < 400
    ? geo.from + x
    : geo.to - (800 - x);
