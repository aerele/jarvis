// The PWA welcome mark under the desktop vitest config (@ is frontend/src here, like the
// other Pwa*.spec.js files, so the PWA modules reach each other by relative paths and only
// @/branding is mocked). The real store, the real BrandMark and the real worn art run; only
// the branding and the network answer are controlled.
import { beforeAll, beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// Branding is reactive so wornAnswer (a computed) sees it change between tests.
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
// The art loader is swapped per test: the real import by default, a stalled one for the
// "import never settles" case.
const art = vi.hoisted(() => ({ load: null, loaded: null }));
vi.mock("../../../pwa/src/lib/holidayWorn", () => ({
	loadWorn: () => art.load(),
	loadedWorn: () => art.loaded,
}));

import HolidayMark from "../../../pwa/src/components/HolidayMark.vue";
import { holidayTouch } from "../../../pwa/src/holidayTouch";

const today = () => new Date().toISOString().slice(0, 10);
const answer = (over = {}) => ({
	v: 1,
	date: today(),
	tz: "UTC",
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	worn: true,
	scene: false,
	may_play: true,
	...over,
});
// The store takes its answer from get_today, so a test sets it the way the page would.
async function today_is(payload) {
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ message: payload }) }))
	);
	await holidayTouch.refresh({ force: true });
}
// The real worn art, loaded once up front (a first dynamic import outlasts a test's flush).
let realArt;
beforeAll(async () => {
	realArt = await import("@jsshared/holiday/worn.mjs");
	realArt.installWornCss();
});
let wrapper;
async function mountMark(props = {}) {
	wrapper = mount(HolidayMark, { props: { size: 56, radius: 14, ...props } });
	await flushPromises();
	return wrapper;
}
const item = () => wrapper.find('[data-testid="holiday-worn"]');

beforeEach(() => {
	globalThis.__brandState.white = false;
	globalThis.__brandState.logo = "";
	art.load = () => Promise.resolve(realArt);
	art.loaded = realArt;
	vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});
afterEach(() => {
	wrapper?.unmount();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe("PWA HolidayMark", () => {
	it("wears the item and is a button on a holiday", async () => {
		await today_is(answer());
		await mountMark();
		expect(item().exists()).toBe(true);
		expect(item().attributes("data-design")).toBe("diwali");
		expect(wrapper.find("button.jvh-button").exists()).toBe(true);
	});

	it("wears nothing under a custom agent name (white label)", async () => {
		await today_is(answer());
		globalThis.__brandState.white = true;
		await mountMark();
		expect(item().exists()).toBe(false);
		expect(wrapper.find("button").exists()).toBe(false);
		expect(wrapper.find(".jv-mark").element.tagName).toBe("SPAN");
	});

	it("shows the brand logo and wears nothing under a logo (white label)", async () => {
		await today_is(answer());
		globalThis.__brandState.white = true;
		globalThis.__brandState.logo = "/files/logo.png";
		await mountMark();
		expect(item().exists()).toBe(false);
		expect(wrapper.find("img.jv-mark-img").exists()).toBe(true);
	});

	it("wears nothing in the upgrade hold, which keeps its sleepy face", async () => {
		await today_is(answer());
		await mountMark({ mood: "upgrading" });
		expect(item().exists()).toBe(false);
		expect(wrapper.find(".jv-mood-upgrading").exists()).toBe(true);
		expect(wrapper.find("button").exists()).toBe(false);
	});

	it.each([
		["no answer", { v: 1, date: today(), tz: "UTC", kind: "none" }],
		["a design the reader does not know", answer({ design: "bogus", occasion: "bogus" })],
		["worn false", answer({ worn: false })],
	])("wears nothing for %s", async (_name, payload) => {
		await today_is(payload);
		await mountMark();
		expect(item().exists()).toBe(false);
		expect(wrapper.find("button").exists()).toBe(false);
	});

	it("smiles for about 2.5 s, then goes back to the resting star", async () => {
		await today_is(answer());
		await mountMark();
		expect(wrapper.find(".jv-mood-happy").exists()).toBe(true);
		vi.advanceTimersByTime(2500);
		await flushPromises();
		expect(wrapper.find(".jv-mood-star").exists()).toBe(true);
		expect(item().exists()).toBe(false);
	});

	it("does not hold the smile for ever when the art import never settles", async () => {
		await today_is(answer());
		art.load = () => new Promise(() => {}); // a stalled chunk
		art.loaded = null;
		await mountMark();
		expect(wrapper.find(".jv-mood-happy").exists()).toBe(true);
		expect(item().exists()).toBe(false); // no art yet
		vi.advanceTimersByTime(3000); // the wait for the art gives up...
		await flushPromises();
		vi.advanceTimersByTime(2500); // ...and the smile runs its normal length
		await flushPromises();
		expect(wrapper.find(".jv-mood-star").exists()).toBe(true);
	});

	it("still ends the smile when the mark is tapped again while the art import is stalled", async () => {
		await today_is(answer());
		art.load = () => new Promise(() => {});
		art.loaded = null;
		await mountMark();
		vi.advanceTimersByTime(1000);
		await wrapper.get("button.jvh-button").trigger("click"); // a replay inside the first 3 s wait
		await flushPromises();
		vi.advanceTimersByTime(2000); // the first wait gives up (it must not cancel the second)
		await flushPromises();
		vi.advanceTimersByTime(1000); // the second wait gives up
		await flushPromises();
		vi.advanceTimersByTime(2500);
		await flushPromises();
		expect(wrapper.find(".jv-mood-star").exists()).toBe(true);
	});
});
