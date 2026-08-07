import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// Reached only through @/stores/shell (the persona row's store), and its ESM
// entry does not resolve under vitest. Same shim the other component specs use.
vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	toast: { error: vi.fn(), success: vi.fn() },
}));

// @/stores/shell reads matchMedia at MODULE level for its narrow-viewport flag,
// which runs on import - before any beforeEach could install a stub. vi.hoisted
// is what gets this in early enough.
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
});

import ModelEffortPicker from "./ModelEffortPicker.vue";

/**
 * "Add a provider" is the picker's one link out to configuration (jarvis#692).
 * The picker itself only ever lists models this tenant can run right now, so
 * these tests pin the two things that keep that true: the row is not a model,
 * and it stays hidden from members who cannot reach the AI models pane (a
 * gated settings section silently falls back to General, so showing it would
 * bounce them).
 */

const POOL = [
	{
		provider: "anthropic",
		models: [
			{ model: "claude-sonnet-4-6", tier: "Primary" },
			{ model: "claude-opus-5", label: "Claude Opus 5", extra: true },
		],
	},
];

function openPicker(props = {}) {
	const w = mount(ModelEffortPicker, {
		props: { modelsByProvider: POOL, defaultModel: "claude-sonnet-4-6", ...props },
	});
	w.find(".mep-pill").trigger("click");
	return w;
}

describe("ModelEffortPicker add-a-provider row", () => {
	it("is hidden by default, since the prop gates on a role the picker cannot check", async () => {
		const w = openPicker();
		await w.vm.$nextTick();
		expect(w.find(".mep-add").exists()).toBe(false);
	});

	it("is hidden for a member who cannot reach the AI models pane", async () => {
		const w = openPicker({ canAddProvider: false });
		await w.vm.$nextTick();
		expect(w.find(".mep-add").exists()).toBe(false);
	});

	it("renders for a user who can configure providers", async () => {
		const w = openPicker({ canAddProvider: true });
		await w.vm.$nextTick();
		const row = w.find(".mep-add");
		expect(row.exists()).toBe(true);
		expect(row.text()).toContain("Add a provider");
	});

	it("reports the intent to the host instead of navigating itself", async () => {
		const w = openPicker({ canAddProvider: true });
		await w.vm.$nextTick();
		await w.find(".mep-add").trigger("click");
		expect(w.emitted("add-provider")).toHaveLength(1);
	});

	it("is not a model choice, so it never emits a selection", async () => {
		const w = openPicker({ canAddProvider: true });
		await w.vm.$nextTick();
		await w.find(".mep-add").trigger("click");
		expect(w.emitted("select-model")).toBeUndefined();
	});

	it("does not join the radio group the model rows form", async () => {
		const withRow = openPicker({ canAddProvider: true });
		const without = openPicker({ canAddProvider: false });
		await withRow.vm.$nextTick();
		await without.vm.$nextTick();
		expect(withRow.findAll('[role="menuitemradio"]')).toHaveLength(
			without.findAll('[role="menuitemradio"]').length
		);
	});

	it("closes the menu on click, so the settings dialog opens over a clean composer", async () => {
		const w = openPicker({ canAddProvider: true });
		await w.vm.$nextTick();
		expect(w.find(".mep-menu").exists()).toBe(true);
		await w.find(".mep-add").trigger("click");
		expect(w.find(".mep-menu").exists()).toBe(false);
	});
});
