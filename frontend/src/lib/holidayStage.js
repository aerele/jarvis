// Small pure rules of the holiday stage layout (components/holiday/HolidayStage.vue).
import { FEST } from "@jsshared/holiday/copy.mjs";
import {
	HERO_FLOOR_GAP_PX,
	WORN_GAP_PX,
	wornOverhangUnits,
} from "@jsshared/holiday/scene-geometry.mjs";
import { hasScene } from "@/lib/holidayScenes";

/**
 * Does the welcome screen use the stage layout? Only on a day or a birthday the server
 * marked as a scene day (the first day of a run, not white label), for a design that has
 * a scene in this build, with the item to wear, on a viewport that is not phone mode and
 * with no maintenance hold when the screen opened. Everything else keeps the normal
 * layout, which already shows the day heading.
 */
export function stageEligible({ answer, phone, whiteLabel, holdAtMount }) {
	if (!answer || phone || whiteLabel || holdAtMount) return false;
	if (answer.kind !== "day" && answer.kind !== "birthday") return false;
	return answer.scene === true && answer.worn === true && hasScene(answer.design);
}

/**
 * The starter cards with the festive greeting card in place of the last one. Never on a
 * birthday (it stays private). The input is not changed.
 */
export function withFestiveCard(cards, answer) {
	const fest = answer && answer.kind === "day" ? FEST[answer.occasion] : null;
	if (!fest || !cards.length) return cards;
	return [...cards.slice(0, -1), { category: "draft", title: fest[0], prompt: fest[1] }];
}

/**
 * How far (px) the worn item reaches above the mark's top, at a mark of `markSize` px. The
 * larger of the design's stated overhang (scene-geometry.mjs WORN_OVERHANG, known before the
 * item is drawn) and `measured`, the real box of the drawn item in tile units (0 when the
 * layer is not mounted yet).
 */
export function wornOverhangPx({ design, markSize, measured = 0 }) {
	const real = Number.isFinite(measured) ? Math.max(0, measured) : 0;
	return (Math.max(wornOverhangUnits(design), real) * markSize) / 56;
}

/** The least y (px below the stage's top) of the mark's top, so the whole item stays in the stage. */
export const wornTop = (overhangPx, gap = WORN_GAP_PX) => (overhangPx > 0 ? overhangPx + gap : 0);

/**
 * The least height (px) of the space above the hero in the layout, so that at rest the item
 * clears the stage's top by `gap` too. `padTop` is the scroller's own top padding, already
 * part of the space above the hero.
 */
export function restLift({ overhangPx, gap = WORN_GAP_PX, padTop = 0 }) {
	return Math.max(0, Math.ceil(wornTop(overhangPx, gap) - padTop));
}

/**
 * How far (px) to shift the hero from its rest place so it is centred in the stage while
 * the scene plays. Positive is down. Its top is held at `minTop` or below, the clearance
 * from the toran (lib/holidayScenes.js sceneClearance), and at `wornTop` or below, so the
 * item worn above the mark stays inside the stage (see wornTop); its bottom inside
 * `maxBottom` so the shift never grows the scroll height; when both cannot hold the
 * clearances win. All sizes are layout sizes in px (they ignore the shift itself).
 *
 * `floorTop` (heroFloorTop in scene-geometry.mjs) is the line the floor row's tall middle
 * part reaches up to at its smallest scale, when the design has one. The hero's bottom is
 * kept HERO_FLOOR_GAP_PX above it, so the row can always stay clear: the hero is centred
 * until that would put it lower, then it sits at that limit. The place is continuous as the
 * stage grows, so a relayout while the scene plays (a font swap, an item's measured reach)
 * moves the hero smoothly, never in a step. A band too small for the hero puts it at the top
 * clearance (the scene then leaves that part of the row out).
 */
export function heroOffset({
	areaH,
	heroH,
	restTop,
	minTop,
	maxBottom,
	wornTop: worn = 0,
	floorTop,
}) {
	const bandTop = Math.max(minTop, worn);
	const bandBottom = Number.isFinite(floorTop) ? floorTop - HERO_FLOOR_GAP_PX : Infinity;
	const lowest = Math.min((areaH - heroH) / 2, maxBottom - heroH, bandBottom - heroH);
	const top = Math.max(bandTop, lowest);
	return Math.round(top - restTop);
}
