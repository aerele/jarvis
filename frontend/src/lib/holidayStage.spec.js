import { describe, it, expect } from "vitest";
import {
	HERO_FLOOR_GAP_PX,
	floorFit,
	groundOf,
	heroFloorTop,
} from "@jsshared/holiday/scene-geometry.mjs";
import { FLOOR_ROW as ONAM_ROW } from "@jsshared/holiday/scenes/onam-art.mjs";
import { FLOOR_ROW as PONGAL_ROW } from "@jsshared/holiday/scenes/pongal-art.mjs";
import { sceneClearance } from "./holidayScenes";
import {
	heroOffset,
	restLift,
	stageEligible,
	withFestiveCard,
	wornOverhangPx,
	wornTop,
} from "./holidayStage";

const DAY = {
	v: 1,
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	date: "2026-11-08",
	tz: "Asia/Kolkata",
	year: 2026,
	worn: true,
	scene: true,
	may_play: true,
};
const ok = (over = {}) => ({
	answer: { ...DAY },
	phone: false,
	whiteLabel: false,
	holdAtMount: false,
	...over,
});

describe("stageEligible", () => {
	it("a scene day, with the item, on a wide screen, no hold: the stage layout", () => {
		expect(stageEligible(ok())).toBe(true);
		expect(stageEligible(ok({ answer: { ...DAY, kind: "birthday", design: "diwali" } }))).toBe(
			true
		);
	});

	it.each([
		["an ordinary day", { answer: { v: 1, kind: "none" } }],
		["a lead-up day", { answer: { ...DAY, kind: "leadup" } }],
		["day 2 of a run (scene false)", { answer: { ...DAY, scene: false } }],
		[
			"a design with no scene in the loader map",
			{ answer: { ...DAY, design: "ramadan", occasion: "ramadan" } },
		],
		["a prototype-key design", { answer: { ...DAY, design: "toString" } }],
		["white label (worn false)", { answer: { ...DAY, worn: false } }],
		["a white-label site", { whiteLabel: true }],
		["phone mode (under 768 px)", { phone: true }],
		["a hold at mount", { holdAtMount: true }],
		["no answer", { answer: null }],
	])("%s: the normal layout", (_, over) => {
		expect(stageEligible(ok(over))).toBe(false);
	});
});

describe("withFestiveCard", () => {
	const cards = [1, 2, 3, 4].map((n) => ({ title: `T${n}`, prompt: `P${n}`, category: "x" }));

	it("replaces the last card with the festival's own, for any number of cards", () => {
		for (const n of [2, 3, 4, 5, 6]) {
			const list = Array.from({ length: n }, (_, i) => ({
				title: `T${i}`,
				prompt: `P${i}`,
			}));
			const out = withFestiveCard(list, DAY);
			expect(out).toHaveLength(n);
			expect(out.slice(0, -1)).toEqual(list.slice(0, -1));
			expect(out.at(-1)).toMatchObject({
				title: "Diwali greetings",
				prompt: "Draft a Diwali greeting for my customers",
			});
		}
	});

	it("uses Republic Day's own wording", () => {
		const out = withFestiveCard(cards, {
			...DAY,
			design: "independence",
			occasion: "republic",
		});
		expect(out.at(-1).title).toBe("Republic Day message");
	});

	it("is absent on a birthday and when there is nothing to replace; the input is untouched", () => {
		expect(withFestiveCard(cards, { ...DAY, kind: "birthday", occasion: "birthday" })).toBe(
			cards
		);
		expect(withFestiveCard([], DAY)).toEqual([]);
		expect(withFestiveCard(cards, { ...DAY, occasion: "nope" })).toBe(cards);
		withFestiveCard(cards, DAY);
		expect(cards.at(-1).title).toBe("T4");
	});
});

describe("heroOffset", () => {
	// Stage 700 high, hero 240 high resting at 100 px; the toran and fireworks need the top
	// at 150 px at least; the scroller's content is 700 px high.
	const base = { areaH: 700, heroH: 240, restTop: 100, minTop: 150, maxBottom: 700 };

	it("centres the hero in the stage: top (700 - 240) / 2 = 230, so 130 px below its rest place", () => {
		expect(heroOffset(base)).toBe(130);
	});

	it("never lets the top rise above the toran and fireworks clearance", () => {
		expect(heroOffset({ ...base, areaH: 420 })).toBe(50); // centred would be 90; held at 150
	});

	it("never lets the hero grow the scroll height: its bottom stays inside the content", () => {
		expect(heroOffset({ ...base, areaH: 1000, maxBottom: 600 })).toBe(260); // top 360
	});

	it("when the clearance and the content cannot both hold, the clearance wins", () => {
		expect(heroOffset({ ...base, minTop: 480, maxBottom: 600 })).toBe(380);
	});

	it("is a whole number of pixels", () => {
		expect(Number.isInteger(heroOffset({ ...base, areaH: 701 }))).toBe(true);
	});

	it("a rest place below the centre gives a negative offset (the hero rises)", () => {
		expect(heroOffset({ ...base, restTop: 400 })).toBe(-170);
	});
});

describe("the worn item above the mark", () => {
	it("wornOverhangPx: the stated overhang at the mark's size, the larger of it and the measured box", () => {
		// Christmas states 36 units; a 72 px mark is 72 / 56 px per unit.
		expect(wornOverhangPx({ design: "christmas", markSize: 56 })).toBe(36);
		expect(wornOverhangPx({ design: "christmas", markSize: 90 })).toBeCloseTo(57.857, 2);
		expect(wornOverhangPx({ design: "christmas", markSize: 56, measured: 40 })).toBe(40);
		expect(wornOverhangPx({ design: "christmas", markSize: 56, measured: 10 })).toBe(36);
	});

	it("wornOverhangPx: nothing for a design with no overhang, and a bad measure is ignored", () => {
		expect(wornOverhangPx({ design: "diwali", markSize: 90 })).toBe(0);
		expect(wornOverhangPx({ design: "diwali", markSize: 56, measured: 18 })).toBe(18);
		expect(wornOverhangPx({ design: "diwali", markSize: 56, measured: NaN })).toBe(0);
		expect(wornOverhangPx({ design: "diwali", markSize: 56, measured: -4 })).toBe(0);
	});

	it("wornTop: the item's reach plus the 12 px gap; nothing when nothing hangs above the mark", () => {
		expect(wornTop(18)).toBe(30);
		expect(wornTop(0)).toBe(0);
	});

	it("restLift: a hat overhanging 18 px needs 18 + 12 px above the mark, less the scroller's own padding", () => {
		expect(restLift({ overhangPx: 18, padTop: 0 })).toBe(30);
		expect(restLift({ overhangPx: 18, padTop: 12 })).toBe(18);
		expect(restLift({ overhangPx: 18.2, padTop: 12 })).toBe(19);
		expect(restLift({ overhangPx: 0, padTop: 12 })).toBe(0);
		expect(restLift({ overhangPx: 4, padTop: 12 })).toBe(4);
	});

	it("heroOffset: a short stage puts the hero below the item's reach, not at the toran line alone", () => {
		// Centred would be 20, the scene clearance 10: the 18 px hat + 12 px gap wins.
		const base = { areaH: 200, heroH: 160, restTop: 12, minTop: 10, maxBottom: 400 };
		expect(heroOffset(base)).toBe(20 - 12);
		expect(heroOffset({ ...base, wornTop: wornTop(18) })).toBe(30 - 12);
	});

	it("heroOffset: the larger of the toran clearance and the item's reach wins", () => {
		const base = { areaH: 200, heroH: 160, restTop: 12, maxBottom: 400 };
		expect(heroOffset({ ...base, minTop: 80, wornTop: 30 })).toBe(80 - 12);
		expect(heroOffset({ ...base, minTop: 20, wornTop: 50 })).toBe(50 - 12);
	});

	it("heroOffset: with no item the result is what it was", () => {
		expect(
			heroOffset({ areaH: 700, heroH: 240, restTop: 100, minTop: 40, maxBottom: 700 })
		).toBe(130);
	});

	it("the glide is between the centred top and the rest top: if both clear the item, every frame does", () => {
		const reach = wornTop(30);
		const rest = Math.max(reach, 12 + 0.6 * 20); // the layout's rest top
		const shift = heroOffset({
			areaH: 300,
			heroH: 200,
			restTop: rest,
			minTop: 0,
			maxBottom: 900,
			wornTop: reach,
		});
		for (let t = 0; t <= 1; t += 0.1) {
			const top = rest + shift * (1 - t); // a linear glide stands for any monotone one
			expect(top).toBeGreaterThanOrEqual(reach - 1e-9);
		}
	});
});

// The floor row and the hero on short stages (heights as the 768, 650 and 600 px windows
// give). The card band is as tall as it is on the real page, so on a short stage it runs
// below the stage's bottom and the row is lifted and shrunk to its smallest.
describe("the hero and the floor row on short stages", () => {
	const ROWS = { pongal: PONGAL_ROW, onam: ONAM_ROW };
	const S = 1.25;

	// What the page does (HolidayStage.vue measureHero), then what the scene's fit makes of it.
	function play({ design, areaH, heroH, cardsBottom, withFloor = true }) {
		const ground = groundOf(cardsBottom, S);
		const floorTop = withFloor
			? heroFloorTop({ design, s: S, hostH: areaH, cardsBottomPx: cardsBottom })
			: undefined;
		const top = heroOffset({
			areaH,
			heroH,
			restTop: 0,
			minTop: sceneClearance(design, S),
			maxBottom: Math.max(areaH, cardsBottom),
			floorTop,
		});
		const bottom = top + heroH;
		const row = ROWS[design];
		const fit = floorFit({
			row,
			ground,
			hostH: areaH,
			s: S,
			clearTop: (bottom + HERO_FLOOR_GAP_PX) / S,
		});
		const coreTop = (fit.anchor - fit.scale * (row.mid - row.rise)) * S;
		return { top, bottom, fit, room: coreTop - bottom };
	}

	it.each([
		["pongal", 768, 250],
		["pongal", 650, 250],
		["pongal", 600, 250],
		["pongal", 600, 300],
		["onam", 768, 250],
		["onam", 650, 250],
		["onam", 600, 250],
		["onam", 600, 300],
	])(
		"%s on a %i px stage with a %i px hero: it clears the row by 16 px, or the row says it is tight",
		(design, areaH, heroH) => {
			for (const cardsBottom of [areaH - 16, 700]) {
				const r = play({ design, areaH, heroH, cardsBottom });
				expect(r.fit.tight || r.room >= HERO_FLOOR_GAP_PX - 0.5).toBe(true);
			}
		}
	);

	it("across the seam where the row starts to need the room, every stage height clears it by the gap or is tight", () => {
		// The hero is centred in the stage on one side of the seam and in the free band on the
		// other, so its place jumps there on purpose: a window resize ends the scene, so no one
		// sees it move. What must hold on both sides is the clearance.
		for (const design of ["pongal", "onam"])
			for (let areaH = 480; areaH <= 760; areaH += 1) {
				const r = play({ design, areaH, heroH: 250, cardsBottom: areaH - 16 });
				expect(
					r.fit.tight || r.room >= HERO_FLOOR_GAP_PX - 0.5,
					`${design} ${areaH}`
				).toBe(true);
			}
	});

	it("a hero the centred stage would put into the row at its smallest is centred in the free band instead", () => {
		const args = { design: "pongal", areaH: 560, heroH: 315, cardsBottom: 700 };
		const before = play({ ...args, withFloor: false });
		expect(before.fit.scale).toBe(0.55);
		expect(before.room).toBeLessThan(HERO_FLOOR_GAP_PX); // the overlap the page used to leave
		const after = play(args);
		expect(after.fit.tight).toBe(false);
		expect(after.room).toBeGreaterThanOrEqual(HERO_FLOOR_GAP_PX - 0.5);
		expect(after.top).toBeLessThan(before.top);
		expect(after.top).toBeGreaterThanOrEqual(sceneClearance("pongal", S)); // not into the thoranam
	});

	it("a band too small for the hero puts it at the top clearance, and the row is then tight", () => {
		const r = play({ design: "pongal", areaH: 500, heroH: 330, cardsBottom: 700 });
		expect(r.top).toBe(Math.round(sceneClearance("pongal", S)));
		expect(r.fit.tight).toBe(true);
	});

	it("a stage with room, or a design with nothing tall under the hero, is centred as before", () => {
		const base = { areaH: 900, heroH: 250, restTop: 0, minTop: 100, maxBottom: 900 };
		const floorTop = heroFloorTop({ design: "pongal", s: S, hostH: 900, cardsBottomPx: 884 });
		expect(heroOffset({ ...base, floorTop })).toBe(heroOffset(base));
		expect(
			heroFloorTop({ design: "christmas", s: S, hostH: 700, cardsBottomPx: 684 })
		).toBeUndefined();
		expect(heroOffset({ ...base, floorTop: undefined })).toBe(heroOffset(base));
	});
});
