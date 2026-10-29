// Where a scene's pieces go on the page: kept clear of the hero, kept at their own edge of
// the stage. Plain functions on the numbers Scene.measure gives (units of the 800-unit stage,
// x centred on 400; px for the boxes the page reports).
import { GAP } from "../scene-geometry.mjs";

const SUBLINE_PX = 420; // the sub-line is wider than the heading; keep clear of it too
const BELOW_HEADING_PX = 44; // and of the sub-line's height

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

/** An x on the 800-unit stage kept the same distance from its own edge of a wider stage. */
export const edgeX = (x, geo) => (x < 400 ? geo.from + x : geo.to - (800 - x));
