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
// The two banner gates read the boot payload and the router; only whether they show matters here.
vi.mock("../../../pwa/src/noticeGate", async () => ({
	showBanner: (await import("vue")).ref(false),
}));
vi.mock("../../../pwa/src/announcementGate", async () => ({
	showAnnouncement: (await import("vue")).ref(false),
}));
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
import { installBannerVisible } from "../../../pwa/src/lib/installBanner";
import { showAnnouncement } from "../../../pwa/src/announcementGate";
import { showBanner } from "../../../pwa/src/noticeGate";
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
const RECT_TABLE = {
	"jvs-host": [0, 0, 390, 726],
	hero: [60, 220, 270, 150],
	mark: [167, 240, 56, 56],
	heading: [80, 310, 230, 26],
	cards: [14, 520, 362, 190],
};
let RECTS;
let hostHeight;
function stubLayout() {
	vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function () {
		const key = this.dataset?.hs || [...this.classList].find((c) => c === "jvs-host");
		const [x, y, w, h0] = RECTS[key] || [0, 0, 0, 0];
		// jsdom has no layout: the art layer is as tall as the page last rendered it.
		const height = key === "jvs-host" ? parseFloat(this.parentElement.style.height) || 0 : h0;
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
function mountStage(props = {}, options = {}) {
	slot = null;
	picked = vi.fn();
	wrapper = mount(HolidayStage, {
		...options,
		props: {
			composerTop: () => hostHeight,
			...props,
		},
		slots: {
			default: (p) => {
				slot = p;
				return [
					h("div", { "data-hs": "hero" }, [
						h("div", { "data-hs": "mark" }),
						h("h1", { "data-hs": "heading" }, p.showDay ? "day" : "normal"),
					]),
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

// jsdom has no ResizeObserver: a fake the test fires by hand.
let observer;
class FakeResizeObserver {
	constructor(callback) {
		this.callback = callback;
		this.targets = new Set();
		observer = this;
	}
	observe(el) {
		this.targets.add(el);
	}
	disconnect() {
		this.targets.clear();
	}
}
const resized = (name) =>
	observer.callback([{ target: wrapper.get(`[data-hs="${name}"]`).element }]);

beforeEach(() => {
	hostHeight = 726;
	RECTS = structuredClone(RECT_TABLE);
	observer = null;
	installBannerVisible.value = false;
	showAnnouncement.value = false;
	showBanner.value = false;
	vi.stubGlobal("ResizeObserver", FakeResizeObserver);
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

	it("a tap on the mark at rest plays the scene again, hiding the starters until it is over", async () => {
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
		expect(cardsHidden()).toBe(true); // the floor row stands in their band
		expect(api.recordHolidayShown).toHaveBeenCalledTimes(1); // a replay is uncounted
		made[1].opts.onBeat("settle", "end");
		await flushPromises();
		expect(cardsHidden()).toBe(false);
	});

	describe("when the page moves under the scene", () => {
		it("watches the hero, the greeting and the starters", async () => {
			await today_is(answer());
			fakeScenes();
			mountStage();
			await flushPromises();
			const names = [...observer.targets].map((el) => el.dataset.hs).sort();
			expect(names).toEqual(["cards", "heading", "hero"]);
		});

		it("reads the boxes again when the starters change size (the personalised ones arrive)", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			RECTS.cards = [14, 470, 362, 240];
			resized("cards");
			await flushPromises();
			expect(made[0].relayout).toHaveBeenCalledTimes(1);
			expect(made[0].relayout.mock.calls[0][0]).toMatchObject({ cards: { y: 470, h: 240 } });
		});

		it("reads them again when the greeting wraps or the font swaps in", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			RECTS.heading = [80, 310, 230, 52];
			resized("heading");
			await flushPromises();
			expect(made[0].relayout.mock.calls[0][0]).toMatchObject({ heading: { h: 52 } });
		});

		it("ignores a change under 1 px", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			RECTS.cards = [14, 520, 362, 190.6];
			resized("cards");
			await flushPromises();
			expect(made[0].relayout).not.toHaveBeenCalled();
		});

		it("does nothing once the scene is over", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			window.dispatchEvent(new Event("pointerdown"));
			RECTS.cards = [14, 470, 362, 240];
			resized("cards");
			await flushPromises();
			expect(made[0].relayout).not.toHaveBeenCalled();
		});

		it("also reads them again during a replay", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			window.dispatchEvent(new Event("pointerdown"));
			window.dispatchEvent(new Event("pointerdown"));
			slot.onMarkTap();
			await flushPromises();
			RECTS.cards = [14, 470, 362, 240];
			resized("cards");
			await flushPromises();
			expect(made[1].relayout).toHaveBeenCalledTimes(1);
		});
	});

	describe("the window changing size", () => {
		const resizeTo = (w, h) => {
			window.innerWidth = w;
			window.innerHeight = h;
			window.dispatchEvent(new Event("resize"));
		};
		beforeEach(() => {
			window.innerWidth = 390;
			window.innerHeight = 844;
		});

		it("does not end a scene when the resize event brings no change of size", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			resizeTo(390, 844);
			expect(made[0].settle).not.toHaveBeenCalled();
		});

		it("ends a scene when the window really changed size", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			resizeTo(844, 390);
			expect(made[0].settle).toHaveBeenCalledWith("early");
		});

		it("does not cost the day's scene when the window settles while the art loads", async () => {
			await today_is(answer());
			const { mod, made } = fakeScenes();
			let arrive;
			art.load = vi.fn(() => new Promise((resolve) => (arrive = () => resolve(mod))));
			mountStage();
			await flushPromises();
			resizeTo(390, 760); // the bar settling at start-up
			expect(cardsHidden()).toBe(true);
			arrive();
			await flushPromises();
			expect(api.recordHolidayShown).toHaveBeenCalledTimes(1);
			expect(made).toHaveLength(1);
		});

		it("compares with the size last seen, so a replay is not ended by an old change", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			window.dispatchEvent(new Event("pointerdown")); // ends it
			resizeTo(390, 760); // at rest: only noted
			window.dispatchEvent(new Event("pointerdown"));
			slot.onMarkTap();
			await flushPromises();
			resizeTo(390, 760); // a spurious second event, same size
			expect(made[1].settle).not.toHaveBeenCalled();
		});
	});

	it("measures the room again for a replay: a rotation since the first play means a smile, not a scene", async () => {
		await today_is(answer());
		const { mod } = fakeScenes();
		mountStage();
		await flushPromises();
		window.dispatchEvent(new Event("pointerdown")); // ends it
		window.dispatchEvent(new Event("pointerdown"));
		hostHeight = 250; // now wider than tall
		slot.onMarkTap();
		await flushPromises();
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(slot.mark.happy).toBe(true);
	});

	it("draws a replay in the layer's new height", async () => {
		await today_is(answer());
		const { made } = fakeScenes();
		mountStage();
		await flushPromises();
		window.dispatchEvent(new Event("pointerdown"));
		window.dispatchEvent(new Event("pointerdown"));
		hostHeight = 640; // the keyboard closed, the bar came back
		slot.onMarkTap();
		await flushPromises();
		expect(made).toHaveLength(2);
		expect(wrapper.get('[data-testid="holiday-art"]').element.style.height).toBe("640px");
	});

	it("measures a tab that was hidden at mount when it is shown, not at mount", async () => {
		await today_is(answer());
		const { mod } = fakeScenes();
		let visibility = "hidden";
		vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
		hostHeight = 0; // nothing laid out in a hidden tab
		mountStage();
		await flushPromises();
		expect(mod.play).not.toHaveBeenCalled();
		hostHeight = 726;
		visibility = "visible";
		document.dispatchEvent(new Event("visibilitychange"));
		await flushPromises();
		expect(api.recordHolidayShown).toHaveBeenCalledTimes(1);
		expect(mod.play).toHaveBeenCalledTimes(1);
	});

	describe("the top banners", () => {
		const banners = {
			install: installBannerVisible,
			announcement: showAnnouncement,
			update: showBanner,
		};

		it.each(Object.keys(banners))(
			"leaves the stage out when the %s banner is up as the screen opens",
			async (name) => {
				await today_is(answer());
				fakeScenes();
				banners[name].value = true;
				mountStage();
				await flushPromises();
				expect(slot.active).toBe(false);
				expect(cardsHidden()).toBe(false);
				expect(api.recordHolidayShown).not.toHaveBeenCalled();
			}
		);

		it("ends a scene when a banner turns up during it", async () => {
			await today_is(answer());
			const { made } = fakeScenes();
			mountStage();
			await flushPromises();
			installBannerVisible.value = true;
			await flushPromises();
			expect(made[0].settle).toHaveBeenCalledWith("early");
		});
	});

	describe("the keyboard on the mark", () => {
		const press = (key, target) =>
			target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));

		it.each(["Enter", " "])(
			"%j on the mark ends the scene without the click it starts replaying it",
			async (key) => {
				await today_is(answer());
				const { mod, made } = fakeScenes();
				mountStage({}, { attachTo: document.body });
				await flushPromises();
				press(key, wrapper.get('[data-hs="mark"]').element);
				expect(made[0].settle).toHaveBeenCalledWith("early");
				slot.onMarkTap(); // the click that follows the keydown
				await flushPromises();
				expect(mod.play).toHaveBeenCalledTimes(1);
			}
		);

		it("a key elsewhere ends the scene and leaves the mark's next tap alone", async () => {
			await today_is(answer());
			const { mod } = fakeScenes();
			mountStage({}, { attachTo: document.body });
			await flushPromises();
			press("Enter", wrapper.get(".starter").element);
			slot.onMarkTap();
			await flushPromises();
			expect(mod.play).toHaveBeenCalledTimes(2);
		});
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
