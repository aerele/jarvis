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
	// The prop defaults to false, so an omitted prop takes this same branch: a
	// host that forgets to pass the gate hides the row rather than exposing it.
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

/**
 * The dashboard builder embeds this same picker in a narrow, overflow-hidden
 * pane whose left edge sits a few px left of the pill. The default geometry
 * (menu hangs off the pill's RIGHT edge and grows leftward, effort flyout
 * opens further left) put the menu 88px outside that pane and the flyout
 * entirely outside it. These pin the two host-selectable modes that keep both
 * inside a narrow host.
 */
describe("ModelEffortPicker narrow-host geometry", () => {
	it("defaults to main chat's end-aligned menu with side flyouts", async () => {
		const w = openPicker();
		await w.vm.$nextTick();
		expect(w.classes()).not.toContain("mep-start");
		expect(w.classes()).not.toContain("mep-compact");
	});

	it("align=start + compact mark the root so the menu and flyout stay inside the host", async () => {
		const w = openPicker({ align: "start", compact: true });
		await w.vm.$nextTick();
		expect(w.classes()).toContain("mep-start");
		expect(w.classes()).toContain("mep-compact");
		// the effort flyout still opens (inline, below its row) and still picks
		await w.find(".mep-sub > .mep-item").trigger("click");
		const fly = w.find(".mep-sub .mep-flyout");
		expect(fly.exists()).toBe(true);
		await fly.findAll(".mep-item").at(-1).trigger("click");
		expect(w.emitted("select-thinking")).toEqual([["high"]]);
	});

	it("rejects an alignment it has no geometry for", () => {
		const validator = ModelEffortPicker.props.align.validator;
		expect(validator("start")).toBe(true);
		expect(validator("end")).toBe(true);
		expect(validator("left")).toBe(false);
	});
});
