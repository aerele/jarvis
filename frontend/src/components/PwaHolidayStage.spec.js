// The PWA scene-day welcome stage under the desktop vitest config (@ is frontend/src here, so
// the PWA modules reach each other by relative paths and only @/branding is mocked). The real
// holiday store, composable and stage run; the art, the network and the page's layout are
// controlled: jsdom has no layout, so getBoundingClientRect answers from a table.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { h } from "vue";

vi.mock("@/branding", async () => {
	const { reactive } = await import("vue");
	const state = reactive({ white: false, logo: "" });
	globalThis.__brandState = state;
	return {
		get isWhitelabeled() {
			return state.white;
		},
		get brandLogoUrl() {
			return state.logo;
		},
		agentName: "Jarvis",
	};
});
const api = vi.hoisted(() => ({ recordHolidayShown: vi.fn() }));
vi.mock("../../../pwa/src/api", () => api);
const art = vi.hoisted(() => ({ load: null }));
vi.mock("../../../pwa/src/lib/holidayScenes", async (original) => ({
	...(await original()),
	loadScene: (design) => art.load(design),
}));

import HolidayStage from "../../../pwa/src/components/HolidayStage.vue";
import { holidayTouch } from "../../../pwa/src/holidayTouch";
import { _resetSpentForTests } from "../../../pwa/src/lib/useHolidayScene";
import { applyTheme } from "../../../pwa/src/lib/theme";

const today = () => new Date().toISOString().slice(0, 10);
const answer = (over = {}) => ({
	v: 1,
	date: today(),
	tz: "UTC",
	year: 2026,
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	worn: true,
	scene: true,
	may_play: true,
	...over,
});
async function today_is(payload) {
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ message: payload }) }))
	);
	await holidayTouch.refresh({ force: true });
}

// The page's layout: a 390 x 726 art layer (844 px screen less the composer), the mark and the
// heading in the middle, the starter cards at the bottom.
const RECTS = {
	"jvs-host": [0, 0, 390, 726],
	mark: [167, 240, 56, 56],
	heading: [80, 310, 230, 26],
	cards: [14, 520, 362, 190],
};
let hostHeight;
function stubLayout() {
	vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function () {
		const key = this.dataset?.hs || [...this.classList].find((c) => c === "jvs-host");
		const [x, y, w, h0] = RECTS[key] || [0, 0, 0, 0];
		const height = key === "jvs-host" ? hostHeight : h0;
		return { x, y, left: x, top: y, width: w, height, right: x + w, bottom: y + height };
	});
}

// A scene that does what the contract says and nothing else.
function fakeScenes() {
	const made = [];
	const mod = {
		play: vi.fn((host, opts) => {
			let finish;
			const done = new Promise((resolve) => (finish = resolve));
			const scene = {
				host,
				opts,
				settle: vi.fn((mode) => {
					opts.onBeat("settle", mode);
					finish();
				}),
				relayout: vi.fn(),
				done,
			};
			made.push(scene);
			return scene;
		}),
	};
	art.load = vi.fn(async () => mod);
	return { mod, made };
}

let wrapper;
let slot; // the latest slot props
let picked; // taps that reached a starter card
function mountStage(props = {}) {
	slot = null;
	picked = vi.fn();
	wrapper = mount(HolidayStage, {
		props: {
			greeting: "Good morning, Priya.",
			words: { heading: "Happy Diwali, Priya.", leadup: null },
			composerTop: () => hostHeight,
			...props,
		},
		slots: {
			default: (p) => {
				slot = p;
				return [
					h("div", { "data-hs": "mark" }),
					h("h1", { "data-hs": "heading" }, p.showDay ? "day" : "normal"),
					h(
						"div",
						{
							"data-hs": "cards",
							class: p.cardsClass,
							style: p.cardsStyle,
							onClickCapture: p.guardCardTap,
						},
						[h("button", { class: "starter", onClick: picked })]
					),
				];
			},
		},
	});
	return wrapper;
}
const cardsHidden = () => wrapper.get('[data-hs="cards"]').classes().includes("jvs-cards-hidden");

function matchMedia({ dark = false, reduced = false } = {}) {
	vi.stubGlobal("matchMedia", (query) => ({
		matches: query.includes("dark") ? dark : query.includes("reduce") ? reduced : false,
		addEventListener() {},
		removeEventListener() {},
	}));
}

beforeEach(() => {
	hostHeight = 726;
	globalThis.__brandState.white = false;
	api.recordHolidayShown.mockReset().mockResolvedValue({ play: true });
	_resetSpentForTests();
	applyTheme("light");
	matchMedia();
	stubLayout();
});
afterEach(() => {
	wrapper?.unmount();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe("PWA HolidayStage", () => {
	it("is just the ordinary welcome on a day with no scene", async () => {
		await today_is(answer({ scene: false }));
		mountStage();
		await flushPromises();
		expect(slot.active).toBe(false);
		expect(slot.mark).toBeNull();
		expect(wrapper.find('[data-testid="holiday-art"]').exists()).toBe(false);
		expect(cardsHidden()).toBe(false);
		expect(api.recordHolidayShown).not.toHaveBeenCalled();
	});

	it("is the ordinary welcome under a custom agent name", async () => {
		await today_is(answer());
		globalThis.__brandState.white = true;
		mountStage();
		await flushPromises();
		expect(slot.active).toBe(false);
	});

	it("hides the starters and keeps the normal greeting from the first render of a scene day", async () => {
		await today_is(answer());
		fakeScenes();
		mountStage();
		expect(slot.active).toBe(true);
		expect(cardsHidden()).toBe(true);
		expect(slot.showDay).toBe(false);
		expect(wrapper.get('[data-hs="heading"]').text()).toBe("normal");
	});

	it("plays on the phone layout in its own art layer that ends at the composer", async () => {
		await today_is(answer());
		const { mod, made } = fakeScenes();
		mountStage();
		await flushPromises();
		expect(api.recordHolidayShown).toHaveBeenCalledTimes(1);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(made[0].opts).toMatchObject({ layout: "phone", year: 2026, reduced: false });
		const layer = wrapper.get('[data-testid="holiday-art"]');
		expect(layer.element.style.height).toBe("726px");
		expect(layer.element.contains(made[0].host)).toBe(true);
		expect(made[0].opts.getBoxes()).toMatchObject({
			host: { w: 390, h: 726 },
			mark: { x: 167, y: 240, w: 56, h: 56 },
			cards: { y: 520 },
		});
	});

	it("puts the art's theme on its own wrapper: pinned, or the system's when not pinned", async () => {
		await today_is(answer());
		fakeScenes();
		applyTheme("dark");
		mountStage();
		await flushPromises();
		expect(wrapper.get('[data-testid="holiday-art"]').attributes("data-theme")).toBe("dark");
		wrapper.unmount();
		applyTheme("system");
		matchMedia({ dark: true });
		mountStage();
		await flushPromises();
		expect(wrapper.get('[data-testid="holiday-art"]').attributes("data-theme")).toBe("dark");
		wrapper.unmount();
		matchMedia({ dark: false });
		mountStage();
		await flushPromises();
		expect(wrapper.get('[data-testid="holiday-art"]').attributes("data-theme")).toBe("light");
	});

	it("rests with no art and no showing used when the screen is landscape or has no room", async () => {
		await today_is(answer());
		const { mod } = fakeScenes();
		hostHeight = 250; // wider than tall
		mountStage();
		await flushPromises();
		expect(art.load).not.toHaveBeenCalled();
		expect(api.recordHolidayShown).not.toHaveBeenCalled();
		expect(mod.play).not.toHaveBeenCalled();
		expect(cardsHidden()).toBe(false);
		expect(slot.showDay).toBe(true);
		expect(slot.mark).toMatchObject({ face: true, item: true });
	});

	it("rests under reduced motion, with no showing used", async () => {
		await today_is(answer());
		fakeScenes();
		matchMedia({ reduced: true });
		mountStage();
		await flushPromises();
		expect(api.recordHolidayShown).not.toHaveBeenCalled();
		expect(cardsHidden()).toBe(false);
	});

	it("does not play when the composer already has a draft", async () => {
		await today_is(answer());
		fakeScenes();
		mountStage({ hasDraft: true });
		await flushPromises();
		expect(art.load).not.toHaveBeenCalled();
		expect(api.recordHolidayShown).not.toHaveBeenCalled();
		expect(cardsHidden()).toBe(false);
	});

	it("rests when the server has no showing left (play: false)", async () => {
		await today_is(answer());
		const { mod } = fakeScenes();
		api.recordHolidayShown.mockResolvedValue({ play: false });
		mountStage();
		await flushPromises();
		expect(mod.play).not.toHaveBeenCalled();
		expect(cardsHidden()).toBe(false);
	});

	it("ends the scene when text lands in the composer", async () => {
		await today_is(answer());
		const { made } = fakeScenes();
		mountStage();
		await flushPromises();
		await wrapper.setProps({ hasDraft: true });
		expect(made[0].settle).toHaveBeenCalledWith("early");
		expect(cardsHidden()).toBe(false);
	});

	it("a touch ends the scene, and the tap that did is not a pick on a starter", async () => {
		await today_is(answer());
		const { made } = fakeScenes();
		mountStage();
		await flushPromises();
		expect(cardsHidden()).toBe(true);
		window.dispatchEvent(new Event("pointerdown"));
		expect(made[0].settle).toHaveBeenCalledWith("early");
		await flushPromises();
		expect(cardsHidden()).toBe(false);
		await wrapper.get(".starter").trigger("click");
		expect(picked).not.toHaveBeenCalled();
		// The next, ordinary tap on a starter works.
		window.dispatchEvent(new Event("pointerdown"));
		await wrapper.get(".starter").trigger("click");
		expect(picked).toHaveBeenCalledTimes(1);
	});

	it.each(["keydown", "touchmove", "wheel"])("a %s ends the scene too", async (type) => {
		await today_is(answer());
		const { made } = fakeScenes();
		mountStage();
		await flushPromises();
		window.dispatchEvent(new Event(type));
		expect(made[0].settle).toHaveBeenCalledWith("early");
	});

	it("a tap on the mark at rest plays the scene again with the starters showing", async () => {
		await today_is(answer());
		const { mod, made } = fakeScenes();
		mountStage();
		await flushPromises();
		window.dispatchEvent(new Event("pointerdown")); // ends it
		window.dispatchEvent(new Event("pointerdown")); // the next gesture, at rest
		slot.onMarkTap();
		await flushPromises();
		expect(mod.play).toHaveBeenCalledTimes(2);
		expect(made[1].opts.replay).toBe(true);
		expect(cardsHidden()).toBe(false);
		expect(api.recordHolidayShown).toHaveBeenCalledTimes(1); // a replay is uncounted
	});

	it("leaves nothing behind when the screen goes away mid-scene", async () => {
		await today_is(answer());
		const { made } = fakeScenes();
		mountStage();
		await flushPromises();
		wrapper.unmount();
		expect(made[0].settle).toHaveBeenCalledWith("instant");
		window.dispatchEvent(new Event("pointerdown")); // no handler left to call settle again
		expect(made[0].settle).toHaveBeenCalledTimes(1);
	});
});
