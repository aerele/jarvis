// How far the worn item reaches above the mark, and the room kept for it. Text only, no
// imports: the Desk widget imports this statically so the hero's room is known before the art
// loads. scene-geometry.mjs re-exports it, so the old import path keeps working.

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
<<<<<<< HEAD

// The worn art is drawn in a tile of this many units, whatever size the tile is on the page.
export const TILE_UNITS = 56;

/** `px` measured on a tile `tilePx` wide, as units of the art's tile (a corner radius, say). */
export function tileUnits(px, tilePx) {
  return (px * TILE_UNITS) / tilePx;
}
=======
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
