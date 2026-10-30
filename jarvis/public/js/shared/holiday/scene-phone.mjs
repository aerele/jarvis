// The phone stage, as plain numbers the welcome page needs before the art arrives. A phone is
// drawn through a window of the same 800-unit stage: the 540 units 130..670 (the approved
// preview's crop), centred on the mark's column (400), at width / 540 px a unit. The scenes
// are the desktop ones; scene-layout.mjs measures the stage through this window and
// scene-place.mjs squeezes the props that are placed at a fixed x into it.
import { MIN_FLOOR_SCALE, sceneHeroTop } from "./scene-geometry.mjs";

export const PHONE = { from: 130, to: 670, units: 540, floorMinPx: 0.5 };

const DESKTOP_MAX_SCALE = 1.25;

/** px per unit on a phone `width` px wide. */
export const phoneScale = (width) =>
  Math.min(width / PHONE.units, DESKTOP_MAX_SCALE);

/**
 * The least scale the floor row may shrink to on a phone: the desktop's, and never so small
 * that a diya (about 18 units across its glow) is under a few px. Below this the row's middle
 * is folded away (scenes with foldMid) instead of shrunk further.
 */
export const phoneFloorMinScale = (s) =>
  Math.max(MIN_FLOOR_SCALE, PHONE.floorMinPx / s);

/**
 * Does `design`'s scene play on this phone screen, or does the screen rest? Only a portrait
 * screen whose mark's top (px from the art layer's top) leaves the scene's top decoration its
 * room at the phone scale, and the worn item (`wornTopPx`) its own. The page asks this before
 * it loads the art and before it counts a showing (record_shown): a rest is not a play.
 */
export function phoneSceneFits({ design, W, H, markTopPx, wornTopPx = 0 }) {
  const sizes = [W, H, markTopPx, wornTopPx];
  if (!sizes.every(Number.isFinite) || !(W > 0) || !(H > W)) return false;
  const need = Math.max(sceneHeroTop(design, phoneScale(W)), wornTopPx);
  return markTopPx >= need;
}
