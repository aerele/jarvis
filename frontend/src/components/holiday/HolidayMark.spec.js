import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";

// jsdom has neither Element.animate nor a real matchMedia; JarvisMark and the worn
// layer only need them to exist.
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

// The store is replaced with a plain ref, so each test just says what today's answer is.
const state = vi.hoisted(() => ({
	answer: null,
	wornFails: false,
	// The art module a test wants loaded instead of the real one, and a gate that holds
	// the load until the test lets it go.
	wornMod: null,
	gate: null,
	// What the page already holds from an earlier load (lib/holidayWorn.loadedWorn).
	loaded: null,
}));
vi.mock("@/holidayTouch", async () => {
	const { ref: vueRef } = await import("vue");
	state.answer = vueRef({ v: 1, kind: "none" });
	return { holidayTouch: { answer: state.answer, start: vi.fn() } };
});
// The real art, through a stand-in for the loader, unless a test says the chunk failed.
vi.mock("@/lib/holidayWorn", () => ({
	loadWorn: vi.fn(async () => {
		if (state.gate) await state.gate;
		if (state.wornFails) return null;
		state.loaded = state.wornMod || (await import("@jsshared/holiday/worn.mjs"));
		return state.loaded;
	}),
	loadedWorn: () => state.loaded,
}));

import HolidayMark from "./HolidayMark.vue";
import JarvisMark from "@/components/JarvisMark.vue";
import { brand } from "@/branding";
import { loadWorn } from "@/lib/holidayWorn";

const DAY = {
	v: 1,
	date: "2026-11-08",
	tz: "Asia/Kolkata",
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	year: null,
	worn: true,
	scene: true,
	may_play: true,
};
const LEADUP = { ...DAY, kind: "leadup", date: "2026-11-06", scene: false };
const layer = (w) => w.find('[data-testid="holiday-worn"]');
const realMatchMedia = window.matchMedia;
// A matchMedia whose reduced-motion query the test can flip and listen to.
function stubMotion(matches) {
	const listeners = new Set();
	const query = {
		matches,
		addEventListener: vi.fn((_, fn) => listeners.add(fn)),
		removeEventListener: vi.fn((_, fn) => listeners.delete(fn)),
		set(value) {
			query.matches = value;
			for (const fn of listeners) fn({ matches: value });
		},
		listeners,
	};
	window.matchMedia = () => query;
	return query;
}

async function mountMark(props = {}, attrs = {}) {
	const w = mount(HolidayMark, { props, attrs });
	await flushPromises();
	return w;
}

describe("HolidayMark", () => {
	// The first dynamic import of the art takes longer than flushPromises waits.
	beforeAll(() => import("@jsshared/holiday/worn.mjs"));
	beforeEach(() => {
		state.answer.value = { ...DAY };
		state.wornFails = false;
		state.wornMod = null;
		state.gate = null;
		state.loaded = null;
		brand.logoUrl = "";
		brand.name = "";
	});
	afterEach(() => {
		vi.useRealTimers();
		vi.mocked(loadWorn).mockClear();
		brand.logoUrl = "";
		brand.name = "";
		window.matchMedia = realMatchMedia;
	});

	describe("the worn item shows only with the face", () => {
		it("wears the item while a mood shows the face", async () => {
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(true);
			expect(layer(w).attributes("data-design")).toBe("diwali");
			expect(layer(w).find(".acc").exists()).toBe(true);
			expect(layer(w).attributes("aria-hidden")).toBe("true");
		});

		it("wears nothing on the resting star", async () => {
			const w = await mountMark({ mood: "star" });
			expect(layer(w).exists()).toBe(false);
		});

		it("shows on a peek and goes away when the peek ends", async () => {
			const w = await mountMark({ mood: "star", peek: false });
			expect(layer(w).exists()).toBe(false);
			await w.setProps({ peek: true });
			expect(layer(w).exists()).toBe(true);
			await w.setProps({ peek: false });
			expect(layer(w).exists()).toBe(false);
		});

		it("shows while a hoverPeek mark is hovered, not before or after", async () => {
			const w = await mountMark({ mood: "star", hoverPeek: true });
			expect(layer(w).exists()).toBe(false);
			await w.find(".jvh").trigger("mouseenter");
			expect(layer(w).exists()).toBe(true);
			await w.find(".jvh").trigger("mouseleave");
			expect(layer(w).exists()).toBe(false);
		});

		it("hoverPeek alone does not show it, and a plain mark ignores hover", async () => {
			const w = await mountMark({ mood: "star" });
			await w.find(".jvh").trigger("mouseenter");
			expect(layer(w).exists()).toBe(false);
		});

		it("reacts when the mood goes happy and leaves when it returns to the star", async () => {
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).attributes("data-state")).toBe("thinking");
			await w.setProps({ mood: "happy" });
			expect(layer(w).attributes("data-state")).toBe("happy");
			await w.setProps({ mood: "star" });
			expect(layer(w).exists()).toBe(false);
		});
	});

	describe("when it wears nothing", () => {
		it("hides it for a brand logo, and keeps the logo", async () => {
			brand.logoUrl = "/files/acme.png";
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jvh").exists()).toBe(false);
			expect(w.find("img.jv-mark-img").exists()).toBe(true);
		});

		it("hides it in the upgrade hold", async () => {
			const w = await mountMark({ mood: "upgrading" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jv-mood-upgrading").exists()).toBe(true);
		});

		it("hides it when the answer says worn is false (white label by name)", async () => {
			state.answer.value = { ...DAY, worn: false };
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jvh").exists()).toBe(false);
		});

		it("hides it on an ordinary day and renders the mark exactly as before", async () => {
			state.answer.value = { v: 1, kind: "none" };
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jvh").exists()).toBe(false);
			// JarvisMark is the root itself, with the props it was given. (Attribute
			// fall-through onto it cannot be asserted here: JarvisMark's own template
			// opens with a comment, which makes it a fragment in dev builds only.)
			expect(w.findComponent(JarvisMark).exists()).toBe(true);
			expect(w.findComponent(JarvisMark).props()).toMatchObject({
				size: 56,
				mood: "thinking",
			});
			expect(w.find(".jv-mark").exists()).toBe(true);
		});

		it("hides it when the answer turns to none while mounted", async () => {
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(true);
			state.answer.value = { v: 1, kind: "none" };
			await flushPromises();
			expect(layer(w).exists()).toBe(false);
		});

		it("leaves the mark plain when the art chunk fails to load", async () => {
			state.wornFails = true;
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jv-mood-thinking").exists()).toBe(true);
		});
	});

	describe("wrapper behaviour", () => {
		it("passes class, style and attributes to the root while wearing", async () => {
			const w = await mountMark(
				{ mood: "thinking" },
				{ class: "mb-6", style: "margin-top: 2px", "data-x": "1" }
			);
			expect(w.element.classList.contains("jvh")).toBe(true);
			expect(w.classes()).toContain("mb-6");
			expect(w.element.getAttribute("data-x")).toBe("1");
			expect(w.element.style.marginTop).toBe("2px");
		});

		it("draws the layer outside the clipped mark", async () => {
			const w = await mountMark({ mood: "thinking" });
			expect(w.find(".jv-mark").element.contains(layer(w).element)).toBe(false);
			expect(w.element.contains(layer(w).element)).toBe(true);
		});

		it("sizes the layer to the mark: 88/56 of its size, offset 16/56", async () => {
			const w = await mountMark({ mood: "thinking", size: 28, radius: 7 });
			const style = layer(w).element.style;
			expect(style.width).toBe("44px");
			expect(style.left).toBe("-8px");
		});

		it("gives every mark its own SVG ids", async () => {
			const a = await mountMark({ mood: "thinking" });
			const b = await mountMark({ mood: "thinking" });
			const ids = (w) => [...layer(w).element.querySelectorAll("[id]")].map((e) => e.id);
			expect(ids(a).length).toBeGreaterThan(0);
			expect(ids(a).filter((id) => ids(b).includes(id))).toEqual([]);
		});
	});

	describe("welcome smile", () => {
		it("smiles once on a lead-up, holds about 2.5 s, then rests", async () => {
			vi.useFakeTimers();
			state.answer.value = { ...LEADUP };
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).attributes("data-state")).toBe("happy");
			await vi.advanceTimersByTimeAsync(2400);
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(200);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			expect(layer(w).exists()).toBe(false);
		});

		it("smiles on the day and on a birthday", async () => {
			for (const kind of ["day", "birthday"]) {
				state.answer.value = {
					...DAY,
					kind,
					design: kind === "day" ? "diwali" : "birthday",
				};
				const w = await mountMark({ welcome: true });
				expect(w.find(".jv-mood-happy").exists(), kind).toBe(true);
				w.unmount();
			}
		});

		it("does not smile twice: a refreshed answer does not restart it", async () => {
			vi.useFakeTimers();
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			await vi.advanceTimersByTimeAsync(3000);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			state.answer.value = { ...DAY };
			await flushPromises();
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
		});

		it("does not smile without the welcome prop, on an ordinary day, or on a logo", async () => {
			expect((await mountMark({})).find(".jv-mood-happy").exists()).toBe(false);
			state.answer.value = { v: 1, kind: "none" };
			expect((await mountMark({ welcome: true })).find(".jv-mood-happy").exists()).toBe(
				false
			);
			state.answer.value = { ...DAY };
			brand.logoUrl = "/files/acme.png";
			expect((await mountMark({ welcome: true })).find(".jv-mood-happy").exists()).toBe(
				false
			);
		});

		it("does not smile during the upgrade hold, and never overrides a real mood", async () => {
			expect(
				(await mountMark({ welcome: true, mood: "upgrading" }))
					.find(".jv-mood-happy")
					.exists()
			).toBe(false);
			const w = await mountMark({ welcome: true, mood: "thinking" });
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			expect(w.find(".jv-mood-thinking").exists()).toBe(true);
		});

		it("leaves no timer behind when it unmounts mid-smile", async () => {
			vi.useFakeTimers();
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			w.unmount();
			expect(vi.getTimerCount()).toBe(0);
		});
	});

	describe("who it wears for", () => {
		it("wears nothing for a custom agent name, even with no logo", async () => {
			brand.name = "Acme Assistant";
			expect(brand.logoUrl).toBe("");
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jvh").exists()).toBe(false);
			expect(w.find(".jv-mood-thinking").exists()).toBe(true);
			// And on the welcome mark: no smile, no button.
			const hero = await mountMark({ welcome: true });
			expect(hero.find(".jv-mood-happy").exists()).toBe(false);
			expect(hero.find("button").exists()).toBe(false);
		});

		it("stops wearing the moment the brand is changed, and wears again when it is cleared", async () => {
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).exists()).toBe(true);
			brand.name = "Acme Assistant";
			await flushPromises();
			expect(layer(w).exists()).toBe(false);
			expect(w.find(".jvh").exists()).toBe(false);
			brand.name = "";
			await flushPromises();
			expect(layer(w).exists()).toBe(true);
		});

		it("forgets a hover when it stops wearing, so it does not wake up hovered", async () => {
			const w = await mountMark({ mood: "star", hoverPeek: true });
			await w.find(".jvh").trigger("mouseenter");
			expect(layer(w).exists()).toBe(true);
			state.answer.value = { v: 1, kind: "none" };
			await flushPromises();
			expect(w.find(".jvh").exists()).toBe(false);
			state.answer.value = { ...DAY };
			await flushPromises();
			expect(w.find(".jvh").exists()).toBe(true);
			expect(layer(w).exists()).toBe(false); // the pointer is not on it
		});
	});

	describe("what it hands to the art", () => {
		const spyArt = () => {
			const wornMarkup = vi.fn(() => '<g class="acc"></g>');
			state.wornMod = { wornMarkup, installWornCss() {} };
			return wornMarkup;
		};

		it("passes small for a mark of 32px or less, and not for a bigger one", async () => {
			const wornMarkup = spyArt();
			await mountMark({ mood: "thinking", size: 28, radius: 7 });
			expect(wornMarkup).toHaveBeenLastCalledWith(
				"diwali",
				expect.objectContaining({ small: true, motion: true, radius: 14 })
			);
			await mountMark({ mood: "thinking", size: 38, radius: 11 });
			expect(wornMarkup).toHaveBeenLastCalledWith(
				"diwali",
				expect.objectContaining({ small: false, motion: true })
			);
			await mountMark({ mood: "thinking", size: 32, radius: 8 });
			expect(wornMarkup.mock.lastCall[1].small).toBe(true);
		});

		it("passes motion false under reduced motion", async () => {
			const wornMarkup = spyArt();
			stubMotion(true);
			await mountMark({ mood: "thinking", size: 28, radius: 7 });
			expect(wornMarkup.mock.lastCall[1].motion).toBe(false);
		});

		it("follows the OS setting while the page is open, and lets go of it on unmount", async () => {
			state.answer.value = { ...DAY, design: "christmas", occasion: "christmas" };
			const query = stubMotion(false);
			const w = await mountMark({ mood: "thinking" });
			expect(layer(w).element.innerHTML).toContain("animateTransform");
			query.set(true);
			await flushPromises();
			expect(layer(w).element.innerHTML).not.toContain("animateTransform");
			query.set(false);
			await flushPromises();
			expect(layer(w).element.innerHTML).toContain("animateTransform");
			expect(query.listeners.size).toBe(1);
			w.unmount();
			expect(query.listeners.size).toBe(0);
		});

		it("listens to nothing when there is nothing to wear", async () => {
			state.answer.value = { v: 1, kind: "none" };
			const query = stubMotion(false);
			await mountMark({ mood: "thinking" });
			expect(query.addEventListener).not.toHaveBeenCalled();
		});
	});

	describe("the art is held for the life of the page", () => {
		it("a mark mounted after the first load draws the item at once, with no new load", async () => {
			state.loaded = await import("@jsshared/holiday/worn.mjs");
			// Marks left mounted by earlier tests react to the answer reset; not what is asked.
			vi.mocked(loadWorn).mockClear();
			const w = mount(HolidayMark, { props: { mood: "thinking" } });
			await nextTick();
			expect(layer(w).exists()).toBe(true);
			expect(loadWorn).not.toHaveBeenCalled();
		});
	});

	describe("how the item arrives", () => {
		it("pops for a hover, a peek, a thinking mark and a smile", async () => {
			const hover = await mountMark({ mood: "star", hoverPeek: true });
			await hover.find(".jvh").trigger("mouseenter");
			expect(layer(hover).classes()).toContain("jvh-pop");
			expect(layer(await mountMark({ peek: true })).classes()).toContain("jvh-pop");
			expect(layer(await mountMark({ mood: "thinking" })).classes()).toContain("jvh-pop");
			expect(layer(await mountMark({ mood: "happy" })).classes()).toContain("jvh-pop");
			expect(layer(await mountMark({ welcome: true })).classes()).toContain("jvh-pop");
		});

		it("only fades in on the mark's own idle blink", async () => {
			vi.useFakeTimers();
			const w = mount(HolidayMark, { props: { mood: "star", idlePeek: true } });
			await flushPromises();
			expect(layer(w).exists()).toBe(false);
			await vi.advanceTimersByTimeAsync(9100); // JarvisMark's idle interval
			expect(w.find(".jv-peek-on").exists()).toBe(true);
			expect(layer(w).exists()).toBe(true);
			expect(layer(w).classes()).not.toContain("jvh-pop");
			w.unmount();
		});
	});

	describe("the welcome mark as a button", () => {
		const button = (w) => w.find("button.jvh");
		const heroMark = (answer, props = {}) => {
			state.answer.value = { ...answer };
			return mountMark({ welcome: true, ...props });
		};

		it("is a real, named button on a lead-up day, the day and a birthday", async () => {
			for (const answer of [
				LEADUP,
				DAY,
				{ ...DAY, kind: "birthday", design: "birthday", occasion: "birthday" },
			]) {
				const w = await heroMark(answer);
				const b = button(w);
				expect(b.exists(), answer.kind).toBe(true);
				expect(b.attributes("type")).toBe("button");
				expect(b.attributes("aria-label")).toBe("Show the greeting again");
				expect(w.element.tagName).toBe("BUTTON");
				w.unmount();
			}
		});

		it("is not a button anywhere else: other marks, an ordinary day, white label, the hold", async () => {
			// A reply avatar or the menu mark: wears, but is not the welcome mark.
			state.answer.value = { ...DAY };
			const avatar = await mountMark({ mood: "thinking" });
			expect(avatar.find("button").exists()).toBe(false);
			expect(avatar.element.tagName).toBe("SPAN");
			// An ordinary day.
			expect((await heroMark({ v: 1, kind: "none" })).find("button").exists()).toBe(false);
			// worn: false (white label by name, as the server decides it).
			expect((await heroMark({ ...DAY, worn: false })).find("button").exists()).toBe(false);
			// The upgrade hold.
			expect((await heroMark(DAY, { mood: "upgrading" })).find("button").exists()).toBe(
				false
			);
			// A logo, and a custom name.
			brand.logoUrl = "/files/acme.png";
			expect((await heroMark(DAY)).find("button").exists()).toBe(false);
			brand.logoUrl = "";
			brand.name = "Acme";
			expect((await heroMark(DAY)).find("button").exists()).toBe(false);
		});

		it("plays the smile again, with the item, when clicked", async () => {
			vi.useFakeTimers();
			state.answer.value = { ...DAY };
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			await vi.advanceTimersByTimeAsync(3000);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			expect(layer(w).exists()).toBe(false);
			await button(w).trigger("click");
			await flushPromises();
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).attributes("data-state")).toBe("happy");
			await vi.advanceTimersByTimeAsync(2400);
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(200);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			w.unmount();
		});

		it("a click during the smile starts it over", async () => {
			vi.useFakeTimers();
			state.answer.value = { ...DAY };
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			await vi.advanceTimersByTimeAsync(2000);
			await button(w).trigger("click");
			await flushPromises();
			await vi.advanceTimersByTimeAsync(2000); // 4 s after mount, 2 s after the click
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(600);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			w.unmount();
		});

		it("under reduced motion a click shows the held smiling frame, without motion", async () => {
			vi.useFakeTimers();
			stubMotion(true);
			const wornMarkup = vi.fn(() => '<g class="acc"></g>');
			state.wornMod = { wornMarkup, installWornCss() {} };
			state.answer.value = { ...DAY };
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			await vi.advanceTimersByTimeAsync(3000);
			await button(w).trigger("click");
			await flushPromises();
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).exists()).toBe(true);
			expect(wornMarkup.mock.lastCall[1].motion).toBe(false);
			w.unmount();
		});

		it("takes class and style like the plain mark does", async () => {
			const w = await mountMark(
				{ welcome: true },
				{ class: "mb-6", style: "flex: none", "data-x": "1" }
			);
			expect(w.element.tagName).toBe("BUTTON");
			expect(w.classes()).toContain("mb-6");
			expect(w.element.getAttribute("style")).toContain("flex");
			expect(w.element.getAttribute("data-x")).toBe("1");
		});
	});

	describe("the smile hold starts when the item shows", () => {
		it("a slow art load does not use up the 2.5 s", async () => {
			vi.useFakeTimers();
			let open;
			state.gate = new Promise((resolve) => (open = resolve));
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			// Smiling already (the face shows at once), item not here yet.
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).exists()).toBe(false);
			await vi.advanceTimersByTimeAsync(10_000);
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			open();
			await flushPromises();
			expect(layer(w).exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(2400);
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(200);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			w.unmount();
		});

		it("smiles for the 2.5 s anyway when the art cannot be loaded", async () => {
			vi.useFakeTimers();
			state.wornFails = true;
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			expect(layer(w).exists()).toBe(false);
			await vi.advanceTimersByTimeAsync(2400);
			expect(w.find(".jv-mood-happy").exists()).toBe(true);
			await vi.advanceTimersByTimeAsync(200);
			expect(w.find(".jv-mood-happy").exists()).toBe(false);
			w.unmount();
		});

		it("leaves nothing behind when it unmounts before the art arrives", async () => {
			vi.useFakeTimers();
			let open;
			state.gate = new Promise((resolve) => (open = resolve));
			const w = mount(HolidayMark, { props: { welcome: true } });
			await flushPromises();
			w.unmount();
			open();
			await flushPromises();
			expect(vi.getTimerCount()).toBe(0);
		});
	});

	describe("the props", () => {
		it("declares every prop JarvisMark has, so a swapped tag loses none", () => {
			const missing = Object.keys(JarvisMark.props).filter(
				(name) => !(name in HolidayMark.props)
			);
			expect(missing).toEqual([]);
		});
	});
});
