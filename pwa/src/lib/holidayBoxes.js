// The boxes a scene reads from the phone welcome, in px relative to the art layer
// (scene contract: jarvis/public/js/shared/holiday/scenes/scene-runtime.mjs). The page
// measures the rects; this adds the starter band the floor row stands in.

const BAND_GAP_PX = 16; // below the hero when there are no starter cards
const BOTTOM_GAP_PX = 8; // above the composer
const SIDE_PX = 14;

const empty = (r) => !r || !(r.w > 0) || !(r.h > 0);

/**
 * `cards` is the starter cards' box, or null/empty when they are not on screen (a screen
 * 640 px tall or less hides them). The band is then what is left between the hero and the
 * composer, so the floor row still has somewhere to stand.
 */
export function sceneBoxes({ host, hero, mark, heading, cards }) {
	if (!empty(cards)) return { host, hero, mark, heading, cards };
	const y = hero.y + hero.h + BAND_GAP_PX;
	const band = {
		x: SIDE_PX,
		y,
		w: host.w - 2 * SIDE_PX,
		h: Math.max(0, host.h - BOTTOM_GAP_PX - y),
	};
	return { host, hero, mark, heading, cards: band };
}
