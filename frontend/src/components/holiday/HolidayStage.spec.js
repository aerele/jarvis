import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";

const state = vi.hoisted(() => ({
	answer: null,
	route: null,
	record: null,
	load: null,
	reduced: false,
	resize: null,
}));
vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	state.route = reactive({ fullPath: "/" });
	return { useRoute: () => state.route };
});
vi.mock("@/holidayTouch", async () => {
	const { ref } = await import("vue");
	state.answer = ref({ v: 1, kind: "none" });
	return { holidayTouch: { answer: state.answer, start: vi.fn() } };
});
vi.mock("@/api", () => ({ recordHolidayShown: (...a) => state.record(...a) }));
vi.mock("@/lib/holidayScenes", () => ({
	hasScene: (d) => d === "diwali",
	loadScene: (...a) => state.load(...a),
	isSceneLoading: () => false,
}));

import HolidayStage from "./HolidayStage.vue";
import { _resetSpentForTests } from "@/lib/useHolidayScene";

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

class FakeObserver {
	constructor(cb) {
		this.cb = cb;
		this.targets = [];
		state.resize = this;
	}
	observe(el) {
		this.targets.push(el);
	}
	disconnect() {
		this.targets = [];
	}
	fire(width, height) {
		this.cb(this.targets.map((target) => ({ target, contentRect: { width, height } })));
	}
	/** One element's size changes (the others are not reported). */
	fireOne(target, width, height) {
		this.cb([{ target, contentRect: { width, height } }]);
	}
}

let sceneApi;
function fakeModule() {
	sceneApi = { settle: vi.fn(), opts: null, finish: null };
	return {
		play: vi.fn((host, opts) => {
			const done = new Promise((resolve) => (sceneApi.finish = resolve));
			sceneApi.opts = opts;
			sceneApi.settle = vi.fn((mode) => {
				opts.onBeat("settle", mode);
				sceneApi.finish();
			});
			// Like the Diwali scene's handle: lets the scene read the boxes again.
			sceneApi.relayout = vi.fn(() => opts.getBoxes());
			return {
				settle: (m) => sceneApi.settle(m),
				relayout: () => sceneApi.relayout(),
				done,
			};
		}),
	};
}

let mod;
function stage(props = {}) {
	return mount(HolidayStage, {
		props: { greeting: "Good morning", firstName: "Priya", ...props },
		slots: {
			sub: '<p class="jv-welcome-sub">Ask about your ERP data.</p>',
			bottom: '<button class="starter">Analyse data</button><a class="calendar">Payment Calendar</a>',
		},
		global: {
			stubs: {
				HolidayMark: {
					name: "HolidayMark",
					props: ["size", "radius", "mood", "stage", "hover"],
					template: '<span class="mark-stub" />',
				},
			},
		},
		attachTo: document.body,
	});
}
// document.fonts double: jsdom has none. ready resolves when `resolve` is called.
function fakeFonts() {
	let resolve;
	const ready = new Promise((r) => (resolve = r));
	Object.defineProperty(document, "fonts", { configurable: true, value: { ready } });
	return resolve;
}
const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
const bottom = (w) => w.get('[data-testid="stage-bottom"]');
const hidden = (w) => bottom(w).classes().includes("is-hidden");
const heading = (w) => {
	const h1 = w.get("h1");
	return document.getElementById(h1.attributes("aria-labelledby")).textContent.trim();
};
const dispatch = (type) => {
	const event = new Event(type, { bubbles: true, cancelable: true });
	const stop = vi.spyOn(event, "stopPropagation");
	window.dispatchEvent(event);
	return { event, stop };
};

// The overall deadline is a promise to the user: written out here, not imported.
const DEADLINE_MS = 2500;

describe("HolidayStage", () => {
	let wrapper;
	beforeEach(() => {
		vi.useFakeTimers();
		_resetSpentForTests();
		localStorage.clear();
		state.answer.value = { ...DAY };
		state.route.fullPath = "/";
		state.reduced = false;
		mod = fakeModule();
		state.load = vi.fn(async () => mod);
		state.record = vi.fn(async () => ({ play: true }));
		window.matchMedia = (q) => ({
			matches: state.reduced && /reduced-motion/.test(q),
			media: q,
			addEventListener() {},
			removeEventListener() {},
		});
		vi.stubGlobal("ResizeObserver", FakeObserver);
		Element.prototype.animate = () => ({ cancel() {} });
		vi.setSystemTime(new Date("2026-11-08T04:00:00Z"));
	});
	afterEach(() => {
		wrapper?.unmount();
		wrapper = null;
		delete document.fonts;
		vi.unstubAllGlobals();
		vi.useRealTimers();
	});

	it("shows the person's normal greeting and hides the bottom block while a scene may play", () => {
		wrapper = stage();
		expect(hidden(wrapper)).toBe(true);
		expect(heading(wrapper)).toBe("Good morning, Priya");
		expect(wrapper.get('[data-testid="holiday-art"]').attributes("aria-hidden")).toBe("true");
	});

	it("keeps the normal greeting until the swap beat, then the day heading is the h1's whole name", async () => {
		wrapper = stage();
		await tick(10);
		expect(heading(wrapper)).toBe("Good morning, Priya");
		sceneApi.opts.onBeat("swap", { instant: false });
		await wrapper.vm.$nextTick();
		expect(heading(wrapper)).toBe("Happy Diwali, Priya");
		expect(wrapper.text()).toContain(
			"Hope you are enjoying the day. I'm here if anything needs doing."
		);
	});

	it("starts the scene on the art layer, and the art layer never takes a pointer event", async () => {
		wrapper = stage();
		await tick(10);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(mod.play.mock.calls[0][0]).toBe(wrapper.get('[data-testid="holiday-art"]').element);
		expect(
			wrapper.get('[data-testid="holiday-art"]').element.closest(".jv-welcome-scroll")
		).toBeNull();
	});

	it("{play: false} gives the rest state: day heading, bottom block shown", async () => {
		state.record = vi.fn(async () => ({ play: false }));
		wrapper = stage();
		await tick(10);
		expect(hidden(wrapper)).toBe(false);
		expect(heading(wrapper)).toBe("Happy Diwali, Priya");
		expect(wrapper.attributes("data-phase")).toBe("rest");
	});

	it("a slow 5 s import: the bottom block is visible by 2.5 s and nothing plays", async () => {
		state.load = vi.fn(() => new Promise((resolve) => setTimeout(() => resolve(mod), 5000)));
		wrapper = stage();
		await tick(DEADLINE_MS - 1);
		expect(hidden(wrapper)).toBe(true);
		await tick(1);
		expect(hidden(wrapper)).toBe(false);
		await tick(5000);
		expect(mod.play).not.toHaveBeenCalled();
		expect(state.record).not.toHaveBeenCalled(); // no slot used without a play
	});

	it("the rest state is the same whether the scene played or not", async () => {
		state.record = vi.fn(async () => ({ play: false }));
		const skipped = stage();
		await tick(10);
		const restText = skipped.text();
		skipped.unmount();
		_resetSpentForTests();
		state.record = vi.fn(async () => ({ play: true }));
		wrapper = stage();
		await tick(10);
		sceneApi.opts.onBeat("swap", { instant: false });
		sceneApi.settle("early");
		await tick(10);
		expect(wrapper.text()).toBe(restText);
		expect(hidden(wrapper)).toBe(false);
	});

	it("the first pointerdown anywhere reveals the cards without being intercepted", async () => {
		wrapper = stage();
		await tick(10);
		const { event, stop } = dispatch("pointerdown");
		await wrapper.vm.$nextTick();
		expect(event.defaultPrevented).toBe(false);
		expect(stop).not.toHaveBeenCalled();
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
		expect(hidden(wrapper)).toBe(false);
		// A second click uses a card: the card is a real, reachable element.
		const card = wrapper.get(".starter");
		const onClick = vi.fn();
		card.element.addEventListener("click", onClick);
		card.trigger("click");
		expect(onClick).toHaveBeenCalled();
	});

	it("a click on a sidebar link mid-scene navigates and the scene settles, nothing prevented", async () => {
		// A link outside the stage, like the sidebar's: its click handler is what router-link
		// does, a route change.
		const link = document.createElement("a");
		link.href = "/jarvis/c/other";
		const navigated = vi.fn((event) => {
			expect(event.defaultPrevented).toBe(false);
			state.route.fullPath = "/c/other";
		});
		link.addEventListener("click", navigated);
		document.body.appendChild(link);
		wrapper = stage();
		await tick(10);
		const down = new Event("pointerdown", { bubbles: true, cancelable: true });
		link.dispatchEvent(down);
		const click = new Event("click", { bubbles: true, cancelable: true });
		link.dispatchEvent(click);
		await wrapper.vm.$nextTick();
		expect(down.defaultPrevented).toBe(false);
		expect(click.defaultPrevented).toBe(false);
		expect(navigated).toHaveBeenCalledTimes(1);
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
		expect(wrapper.attributes("data-phase")).toBe("rest");
		expect(hidden(wrapper)).toBe(false);
		link.remove();
	});

	it("the replay button passes the pointer's hover on to the mark under it", async () => {
		wrapper = stage();
		await tick(10);
		const button = wrapper.get('[data-testid="stage-replay"]');
		const markStub = () => wrapper.findComponent({ name: "HolidayMark" });
		expect(markStub().props("hover")).toBe(false);
		await button.trigger("mouseenter");
		expect(markStub().props("hover")).toBe(true);
		await button.trigger("mouseleave");
		expect(markStub().props("hover")).toBe(false);
	});

	it("the first key is not intercepted and focus stays where it was (the composer)", async () => {
		const composer = document.createElement("textarea");
		document.body.appendChild(composer);
		composer.focus();
		wrapper = stage();
		await tick(10);
		const { event, stop } = dispatch("keydown");
		expect(event.defaultPrevented).toBe(false);
		expect(stop).not.toHaveBeenCalled();
		expect(document.activeElement).toBe(composer);
		await wrapper.vm.$nextTick();
		expect(hidden(wrapper)).toBe(false);
		composer.remove();
	});

	it("a size change mid-scene settles it, and it never restarts", async () => {
		wrapper = stage();
		await tick(10);
		state.resize.fire(900, 600); // the first reading: the baseline
		expect(sceneApi.settle).not.toHaveBeenCalled();
		state.resize.fire(700, 600);
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
		state.resize.fire(900, 600);
		state.resize.fire(1000, 600);
		await tick(DEADLINE_MS * 2);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(hidden(wrapper)).toBe(false);
	});

	it("a hero size change (the font swap) mid-scene keeps it playing and reads the boxes again", async () => {
		wrapper = stage();
		await tick(10);
		const hero = wrapper.get(".jv-stage-hero").element;
		state.resize.fireOne(hero, 680, 228); // the first reading: the baseline
		const read = vi.spyOn(Element.prototype, "getBoundingClientRect");
		state.resize.fireOne(hero, 680, 250);
		expect(sceneApi.settle).not.toHaveBeenCalled();
		expect(sceneApi.relayout).toHaveBeenCalledTimes(1);
		expect(read).toHaveBeenCalled();
		expect(wrapper.attributes("data-phase")).toBe("playing");
		expect(hidden(wrapper)).toBe(true);
		read.mockRestore();
	});

	it("the bottom block changing (cards arriving) keeps the scene playing", async () => {
		wrapper = stage();
		await tick(10);
		const block = bottom(wrapper).element;
		state.resize.fireOne(block, 680, 0);
		state.resize.fireOne(block, 680, 260);
		expect(sceneApi.settle).not.toHaveBeenCalled();
		expect(wrapper.attributes("data-phase")).toBe("playing");
	});

	it("a stage area change (height only) settles it", async () => {
		wrapper = stage();
		await tick(10);
		const area = wrapper.element;
		state.resize.fireOne(area, 900, 600);
		state.resize.fireOne(area, 900, 500);
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
	});

	it("fonts not ready until 2.6 s: no scene, the cards show at 2.5 s, no slot used", async () => {
		const ready = fakeFonts();
		wrapper = stage();
		await tick(DEADLINE_MS - 1);
		expect(hidden(wrapper)).toBe(true);
		await tick(1);
		expect(hidden(wrapper)).toBe(false);
		ready();
		await tick(200);
		expect(mod.play).not.toHaveBeenCalled();
		expect(state.record).not.toHaveBeenCalled();
	});

	it("fonts ready at 1 s: the scene starts then, not before", async () => {
		const ready = fakeFonts();
		wrapper = stage();
		await tick(900);
		expect(mod.play).not.toHaveBeenCalled();
		expect(state.record).not.toHaveBeenCalled();
		await tick(100);
		ready();
		await tick(10);
		expect(state.record).toHaveBeenCalledTimes(1);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(wrapper.attributes("data-phase")).toBe("playing");
	});

	it("typing in the composer settles it", async () => {
		wrapper = stage();
		await tick(10);
		await wrapper.setProps({ composerEmpty: false });
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
		expect(hidden(wrapper)).toBe(false);
	});

	it("a hold starting mid-scene settles it, and the replay button goes", async () => {
		wrapper = stage();
		await tick(10);
		expect(wrapper.find('[data-testid="stage-replay"]').exists()).toBe(true);
		await wrapper.setProps({ holdActive: true });
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
		expect(wrapper.find('[data-testid="stage-replay"]').exists()).toBe(false);
	});

	it("a route change settles it", async () => {
		wrapper = stage();
		await tick(10);
		state.route.fullPath = "/c/other";
		await wrapper.vm.$nextTick();
		expect(sceneApi.settle).toHaveBeenCalledWith("early");
	});

	it("leaving the screen ends the scene instantly and leaves no timer", async () => {
		wrapper = stage();
		await tick(10);
		wrapper.unmount();
		wrapper = null;
		expect(sceneApi.settle).toHaveBeenCalledWith("instant");
		expect(vi.getTimerCount()).toBe(0);
	});

	it("a second new chat after a reply does not hide the cards again", async () => {
		state.record = vi.fn(async () => ({ play: false }));
		const first = stage();
		await tick(10);
		first.unmount();
		state.record = vi.fn();
		wrapper = stage();
		expect(hidden(wrapper)).toBe(false);
		expect(state.record).not.toHaveBeenCalled();
	});

	it("reduced motion shows the rest state at first paint and records nothing", async () => {
		state.reduced = true;
		wrapper = stage();
		expect(hidden(wrapper)).toBe(false);
		expect(heading(wrapper)).toBe("Happy Diwali, Priya");
		await tick(10);
		expect(state.record).not.toHaveBeenCalled();
		expect(mod.play).not.toHaveBeenCalled();
	});

	it("the mark is a labelled replay button over the mark's box; at rest a click replays", async () => {
		state.record = vi.fn(async () => ({ play: false }));
		wrapper = stage();
		await tick(10);
		const button = wrapper.get('[data-testid="stage-replay"]');
		expect(button.attributes("aria-label")).toBe("Replay the greeting");
		expect(button.element.parentElement.classList.contains("jv-stage-mark")).toBe(true);
		await button.trigger("click");
		await tick(10);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(mod.play.mock.calls[0][1].replay).toBe(true);
		expect(hidden(wrapper)).toBe(false);
	});

	it.each([
		[1170, 90, 1.25], // 1440 screen with the sidebar open: capped
		[1600, 90, 1.25], // 1920
		[2200, 90, 1.25], // 2560
		[640, 58, 0.8],
	])(
		"scale: a %i px welcome area gives a %i px mark (factor %f), never over 96 / 40",
		async (area, mark, factor) => {
			const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
			Object.defineProperty(HTMLElement.prototype, "clientWidth", {
				configurable: true,
				get: () => area,
			});
			try {
				wrapper = stage();
				await wrapper.vm.$nextTick();
				const box = wrapper.get(".jv-stage-mark").element;
				expect(box.style.width).toBe(`${mark}px`);
				expect(wrapper.attributes("style")).toContain(`--s: ${factor}`);
				expect(mark).toBeLessThanOrEqual(96);
				expect(32 * factor).toBeLessThanOrEqual(40);
			} finally {
				if (original)
					Object.defineProperty(HTMLElement.prototype, "clientWidth", original);
				else delete HTMLElement.prototype.clientWidth;
			}
		}
	);
});
