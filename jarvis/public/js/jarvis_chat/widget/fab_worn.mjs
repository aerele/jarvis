// Pure rules for the item the launcher wears on a holiday (Widget.vue draws it, FabWorn.vue
// holds the layer). Text only: no art import, so this stays in the widget bundle.

import { TILE_UNITS } from "../../shared/holiday/worn-room.mjs";

// The launcher tile, matches .jvw-fab and fab_position.FAB_SIZE (holiday_geometry.test.mjs
// holds these to the CSS).
export const FAB_PX = 54;
export const FAB_RADIUS_PX = 16;
export const MARGIN_UNITS = 16; // room the art keeps around the tile on every side

/**
 * Does the item show right now? Only with the face (the item and the face come and go
 * together), never while the launcher is dragged or the panel is open (the panel covers
 * the launcher's job), never on a brand logo (no face to wear it on) and never in the
 * upgrade hold (its sleepy face is telling the user something).
 */
export function isItemShown({
  itemReady,
  faceShown,
  dragging,
  panelOpen,
  brandLogo,
  maintenance,
}) {
  return (
    !!itemReady &&
    !!faceShown &&
    !dragging &&
    !panelOpen &&
    !brandLogo &&
    !maintenance
  );
}

/** Where the item's svg sits against a tile of `px` (the launcher by default): the art's own 88 unit viewBox. */
export function wornLayerBox(px = FAB_PX) {
  const unit = px / TILE_UNITS;
  return {
    left: `${-MARGIN_UNITS * unit}px`,
    top: `${-MARGIN_UNITS * unit}px`,
    width: `${(TILE_UNITS + 2 * MARGIN_UNITS) * unit}px`,
    height: `${(TILE_UNITS + 2 * MARGIN_UNITS) * unit}px`,
  };
}

/**
 * CSS variables that point the face at the item, from the art's LOOK_AT table (tile units).
 * {} for a design with no entry: its default glance stays.
 */
export function lookStyle(lookAt, design) {
  if (!lookAt || !Object.hasOwn(lookAt, design)) return {};
  const [x, y] = lookAt[design];
  const unit = FAB_PX / TILE_UNITS;
  return {
    "--jvw-lx": `${x * unit}px`,
    "--jvw-ly": `${y * unit}px`,
    "--jvw-look": `jvh-look-${design}`,
  };
}
