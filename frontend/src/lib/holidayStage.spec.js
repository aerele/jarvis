import { describe, it, expect } from "vitest";
import { stageEligible, withFestiveCard } from "./holidayStage";

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
