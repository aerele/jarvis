// How much room the hero keeps above the mark for the worn item, which hangs past the
// tile's top. The table is the shared one the desktop and the widget use; a relative
// import so bare `node --test` can run this file.
import {
	WORN_GAP_PX,
	WORN_OVERHANG,
} from "../../../jarvis/public/js/shared/holiday/worn-room.mjs";

/** Pixels of extra room (the item's reach and a gap), or 0 when nothing hangs above. */
export function heroRoomPx(worn, hold) {
	const overhang = worn && !hold ? WORN_OVERHANG[worn.design] : 0;
	return overhang ? overhang + WORN_GAP_PX : 0;
}
