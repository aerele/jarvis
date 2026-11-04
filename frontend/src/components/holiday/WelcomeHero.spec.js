import { describe, it, expect, vi, beforeEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { mount, flushPromises } from "@vue/test-utils";

vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
	Element.prototype.animate = () => ({ cancel() {}, finished: Promise.resolve() });
});

const state = vi.hoisted(() => ({ answer: null }));
vi.mock("@/holidayTouch", async () => {
	const { ref } = await import("vue");
	state.answer = ref({ v: 1, kind: "none" });
	return { holidayTouch: { answer: state.answer, start: vi.fn() } };
});
vi.mock("@/lib/holidayWorn", () => ({
	loadWorn: vi.fn(async () => null),
	loadedWorn: () => null,
}));

import WelcomeHero from "./WelcomeHero.vue";

const base = {
	v: 1,
	date: "2026-11-08",
	tz: "Asia/Kolkata",
	year: null,
	worn: true,
	scene: false,
	may_play: true,
};
const DAY = { ...base, kind: "day", design: "diwali", occasion: "diwali" };
const LEADUP = { ...base, kind: "leadup", design: "diwali", occasion: "diwali" };

const hero = (props = {}) =>
	mount(WelcomeHero, { props: { greeting: "Good morning", firstName: "Priya", ...props } });
const text = (w) => w.get('[data-testid="welcome-greeting"]').text();

describe("WelcomeHero", () => {
	beforeEach(() => {
		state.answer.value = { v: 1, kind: "none" };
	});

	it("an ordinary day keeps today's greeting and shows no advance wish", () => {
		const w = hero();
		expect(text(w)).toBe("Good morning, Priya");
		expect(w.find('[data-testid="welcome-leadup"]').exists()).toBe(false);
	});

	it("on a lead-up day the greeting stays and the advance wish renders under it", () => {
		state.answer.value = { ...LEADUP };
		const w = hero();
		expect(text(w)).toBe("Good morning, Priya");
		expect(w.get('[data-testid="welcome-leadup"]').text()).toBe(
			"Wishing you a happy Diwali in advance!"
		);
		// Order: the heading row (mark and h1), then the wish.
		const kids = [...w.element.children].map((e) => e.tagName);
		expect(kids).toEqual(["DIV", "P"]);
		expect(w.element.children[0].contains(w.get("h1").element)).toBe(true);
	});

	it("on the day the heading is the day heading and there is no advance wish", () => {
		state.answer.value = { ...DAY };
		const w = hero();
		expect(text(w)).toBe("Happy Diwali, Priya");
		expect(w.find('[data-testid="welcome-leadup"]').exists()).toBe(false);
	});

	it("a birthday heading, and Republic Day uses its own words", () => {
		state.answer.value = {
			...base,
			kind: "birthday",
			design: "birthday",
			occasion: "birthday",
		};
		expect(text(hero())).toBe("Happy birthday, Priya");
		state.answer.value = {
			...base,
			kind: "day",
			design: "independence",
			occasion: "republic",
		};
		expect(text(hero())).toBe("Happy Republic Day, Priya");
	});

	it("a white-label site (worn: false) still gets the words", () => {
		state.answer.value = { ...DAY, worn: false };
		expect(text(hero())).toBe("Happy Diwali, Priya");
		state.answer.value = { ...LEADUP, worn: false };
		const w = hero();
		expect(w.get('[data-testid="welcome-leadup"]').exists()).toBe(true);
	});

	it("the day heading holds in the upgrade hold", () => {
		state.answer.value = { ...DAY };
		expect(text(hero({ mood: "upgrading" }))).toBe("Happy Diwali, Priya");
	});

	it("a long first name is shown whole", () => {
		state.answer.value = { ...DAY };
		expect(text(hero({ firstName: "Venkatasubramanian" }))).toBe(
			"Happy Diwali, Venkatasubramanian"
		);
	});

	it("an answer that goes stale mid-visit puts the ordinary greeting back", async () => {
		state.answer.value = { ...DAY };
		const w = hero();
		state.answer.value = { v: 1, kind: "none" };
		await flushPromises();
		expect(text(w)).toBe("Good morning, Priya");
	});

	describe("the name", () => {
		it("an empty or blank first name leaves no dangling comma", () => {
			for (const firstName of ["", "   ", undefined]) {
				expect(text(hero({ firstName })), JSON.stringify(firstName)).toBe("Good morning");
			}
			state.answer.value = { ...DAY };
			for (const firstName of ["", "  "]) {
				expect(text(hero({ firstName }))).toBe("Happy Diwali");
			}
		});

		it("trims the name", () => {
			expect(text(hero({ firstName: "  Priya " }))).toBe("Good morning, Priya");
			state.answer.value = { ...DAY };
			expect(text(hero({ firstName: "  Priya " }))).toBe("Happy Diwali, Priya");
		});
	});

	describe("the heading and the mark", () => {
		it("the h1 is only the greeting: no aria-labelledby, no id, nothing else inside", async () => {
			for (const answer of [{ v: 1, kind: "none" }, LEADUP, DAY]) {
				state.answer.value = { ...answer };
				const w = hero();
				await flushPromises();
				const h1 = w.get("h1");
				expect(h1.attributes("aria-labelledby")).toBeUndefined();
				expect(h1.element.querySelector("[id]")).toBeNull();
				expect(h1.element.querySelector("button, svg, .jv-mark")).toBeNull();
				expect(h1.text()).toBe(text(w));
			}
		});

		it("on a holiday the mark is a button beside the h1, not in it, and the item is not read", async () => {
			state.answer.value = { ...DAY };
			const w = hero();
			await flushPromises();
			const button = w.get("button");
			expect(button.attributes("aria-label")).toBe("Show the greeting again");
			expect(w.get("h1").element.contains(button.element)).toBe(false);
			expect(w.get("h1").text()).toBe("Happy Diwali, Priya");
		});

		it("an ordinary day has no button at all", () => {
			expect(hero().find("button").exists()).toBe(false);
		});

		it("the lead-up line is typed like the orienting line under it", () => {
			// jsdom has no layout, so this reads the rule from the source: same size, colour,
			// line height and top margin as ChatView's .jv-welcome-sub, same phone step-down.
			const hero = fs.readFileSync(path.resolve(__dirname, "WelcomeHero.vue"), "utf8");
			const chat = fs.readFileSync(
				path.resolve(__dirname, "../../views/ChatView.vue"),
				"utf8"
			);
			const rule = (src, selector, from = 0) =>
				new RegExp(`${selector.replace(".", "\\.")}\\s*\\{([^}]*)\\}`).exec(
					src.slice(from)
				)?.[1];
			const norm = (css) => css.replace(/\s+/g, " ").trim();
			expect(norm(rule(hero, ".jv-hero-leadup"))).toBe(norm(rule(chat, ".jv-welcome-sub")));
			const phone = (src) => src.slice(src.lastIndexOf("@media (max-width: 640px)"));
			expect(norm(rule(phone(hero), ".jv-hero-leadup"))).toBe(
				norm(rule(phone(chat), ".jv-welcome-sub"))
			);
		});
	});
});
