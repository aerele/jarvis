import { describe, it, expect } from "vitest";
import { heroOffset, stageEligible, withFestiveCard } from "./holidayStage";

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
			{ answer: { ...DAY, design: "pongal", occasion: "pongal" } },
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
