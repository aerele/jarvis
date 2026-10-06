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

// A proxy-only workspace's model cannot think, so the server lists no effort levels
// (jarvis-admin-v2#648). The picker must not offer a choice the turn would drop.
describe("ModelEffortPicker effort row", () => {
	const effortRow = (w) => w.findAll(".mep-item").filter((b) => b.text().startsWith("Effort"));

	it("is offered when the server lists thinking levels", async () => {
		const w = openPicker({ thinkingLevels: ["low", "medium", "high"] });
		await w.vm.$nextTick();
		expect(effortRow(w)).toHaveLength(1);
		expect(w.find(".mep-div").exists()).toBe(true);
	});

	it("is hidden, with its divider, when the server lists none", async () => {
		const w = openPicker({ thinkingLevels: [] });
		await w.vm.$nextTick();
		expect(w.findAll(".mep-item").length).toBeGreaterThan(0);
		expect(effortRow(w)).toHaveLength(0);
		expect(w.find(".mep-div").exists()).toBe(false);
	});

	it("drops a stored level from the pill when no level is offered", () => {
		const w = mount(ModelEffortPicker, {
			props: { modelsByProvider: POOL, thinkingOverride: "high", thinkingLevels: [] },
		});
		expect(w.find(".mep-effort").classes()).toContain("mep-hide");
		expect(w.find(".mep-dot").classes()).toContain("mep-hide");
	});
});

const ADMIN_TIP = "Sign-in expired. Reconnect it in AI models.";
const MEMBER_TIP = "Sign-in expired. Ask your workspace admin to reconnect it.";
const MAP = { "gpt-5.6": { upstream: "codex", label: "ChatGPT" } };
const EXPIRED_GROUPS = [{ provider: "openai", models: [{ model: "gpt-5.6" }, { model: "claude-x" }] }];

function buildExpired(props = {}) {
	return mount(ModelEffortPicker, {
		props: { modelsByProvider: EXPIRED_GROUPS, expiredModels: MAP, ...props },
	});
}
const dot = (w) => w.find('[data-testid="mep-expired-dot"]');
const pill = (w) => w.find(".mep-pill");

describe("ModelEffortPicker expired sign-in", () => {
	it("shows the dot and admin tooltip for an expired explicit pick", () => {
		const w = buildExpired({ modelOverride: "gpt-5.6", canAddProvider: true });
		expect(dot(w).exists()).toBe(true);
		expect(dot(w).classes()).toEqual(
			expect.arrayContaining(["size-1.5", "shrink-0", "rounded-full", "bg-surface-red-5"])
		);
		expect(pill(w).attributes("title")).toBe(ADMIN_TIP);
	});

	it("shows the member tooltip when the viewer cannot add providers", () => {
		const w = buildExpired({ modelOverride: "gpt-5.6", canAddProvider: false });
		expect(pill(w).attributes("title")).toBe(MEMBER_TIP);
	});

	it("matches a prefixed id against a bare map key", () => {
		const w = buildExpired({ modelOverride: "openai/gpt-5.6" });
		expect(dot(w).exists()).toBe(true);
	});

	it("shows the dot when the pill names an expired default model", () => {
		const w = buildExpired({ modelOverride: "", defaultModel: "gpt-5.6", canAddProvider: true });
		expect(w.find(".mep-model").text()).toBe("gpt-5.6");
		expect(dot(w).exists()).toBe(true);
		expect(pill(w).attributes("title")).toBe(ADMIN_TIP);
	});

	it("shows no dot on Auto", () => {
		const w = buildExpired({ modelOverride: "", defaultModel: "", canAddProvider: true });
		expect(w.find(".mep-model").text()).toBe("Auto");
		expect(dot(w).exists()).toBe(false);
		expect(pill(w).attributes("title")).toBe("Model and effort");
	});

	it("shows no dot for a model that is not expired", () => {
		const w = buildExpired({ modelOverride: "claude-x" });
		expect(dot(w).exists()).toBe(false);
		expect(pill(w).attributes("title")).toBe("Model and effort");
	});

	it("marks an expired menu row and keeps it clickable", async () => {
		const w = buildExpired({ modelOverride: "claude-x" });
		await pill(w).trigger("click");
		const rows = w.findAll(".mep-item").filter((r) => r.text().includes("gpt-5.6"));
		expect(rows).toHaveLength(1);
		expect(rows[0].find(".mep-desc").text()).toBe("Sign-in expired");
		await rows[0].trigger("click");
		expect(w.emitted("select-model")[0]).toEqual(["gpt-5.6"]);
		const other = buildExpired({ modelOverride: "claude-x" });
		await pill(other).trigger("click");
		expect(other.text()).toContain("claude-x");
		expect(
			other.findAll(".mep-desc").filter((d) => d.text() === "Sign-in expired")
		).toHaveLength(1);
	});
});
