import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i :data-icon="name" />` },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));

import AgentModelPicker from "./AgentModelPicker.vue";

const ELIGIBLE = {
	agent: "close-auditor",
	required_tier: "Advanced",
	catalog: "ok",
	models: [
		{
			provider: "anthropic",
			model: "claude-opus-5",
			label: "Claude Opus 5",
			capability_tier: "Frontier",
			cost_note: "$5.00 in / $15.00 out per 1M tokens",
		},
		{
			provider: "anthropic",
			model: "claude-sonnet-5",
			label: "Claude Sonnet 5",
			capability_tier: "Advanced",
			cost_note: "$1.00 in / $3.00 out per 1M tokens",
		},
		{
			provider: "openai",
			model: "gpt-5",
			label: "GPT-5",
			capability_tier: "Advanced",
			cost_note: "",
		},
	],
};

describe("AgentModelPicker grouping", () => {
	it("groups options by provider, best-first order preserved", () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE } });
		const heads = w.findAll(".uppercase").map((h) => h.text());
		expect(heads).toEqual(["Anthropic", "Openai"]);
		const buttons = w.findAll("[aria-pressed]");
		expect(buttons).toHaveLength(3);
		expect(buttons[0].text()).toContain("Claude Opus 5");
		expect(buttons[1].text()).toContain("Claude Sonnet 5");
		expect(buttons[2].text()).toContain("GPT-5");
	});

	it("shows each option's capability tier badge and cost note", () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE } });
		const first = w.findAll("[aria-pressed]")[0];
		expect(first.find(".badge").attributes("data-theme")).toBe("green"); // Frontier
		expect(first.text()).toContain("$5.00 in / $15.00 out per 1M tokens");
	});
});

describe("AgentModelPicker selection", () => {
	it("emits select with the exact {provider, model} on click", async () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE } });
		await w.findAll("[aria-pressed]")[1].trigger("click");
		expect(w.emitted("select")).toEqual([
			[{ provider: "anthropic", model: "claude-sonnet-5" }],
		]);
	});

	it("marks the current selection aria-pressed and shows a check mark", () => {
		const w = mount(AgentModelPicker, {
			props: { eligible: ELIGIBLE, current: { provider: "openai", model: "gpt-5" } },
		});
		const options = w.findAll("[aria-pressed]");
		expect(options[2].attributes("aria-pressed")).toBe("true");
		expect(options[0].attributes("aria-pressed")).toBe("false");
		expect(options[2].find('[data-icon="check"]').exists()).toBe(true);
	});

	it("every option is its own Tab stop (no radiogroup/roving-tabindex contract)", () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE } });
		expect(w.find('[role="radiogroup"]').exists()).toBe(false);
		expect(w.find('[role="radio"]').exists()).toBe(false);
		for (const b of w.findAll("[aria-pressed]")) {
			expect(b.attributes("tabindex")).toBeUndefined(); // native button default (0), untouched
		}
	});
});

describe("AgentModelPicker saving (in-flight pick)", () => {
	it("disables every option and marks the list aria-busy while a pick is in flight", () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE, saving: true } });
		expect(w.find("[aria-busy]").attributes("aria-busy")).toBe("true");
		for (const b of w.findAll("[aria-pressed]")) {
			expect(b.attributes("disabled")).toBeDefined();
		}
	});

	it("a click while saving never emits select", async () => {
		const w = mount(AgentModelPicker, { props: { eligible: ELIGIBLE, saving: true } });
		await w.findAll("[aria-pressed]")[0].trigger("click");
		expect(w.emitted("select")).toBeUndefined();
	});
});

describe("AgentModelPicker non-happy states", () => {
	it("shows a loading state", () => {
		const w = mount(AgentModelPicker, { props: { eligible: null, loading: true } });
		expect(w.text()).toContain("Loading models…");
		expect(w.findAll("[aria-pressed]")).toHaveLength(0);
	});

	it("shows the catalog-unknown message, never claims none eligible", () => {
		const w = mount(AgentModelPicker, {
			props: { eligible: { catalog: "unknown", models: [] } },
		});
		expect(w.text()).toContain("Model list unavailable — try again shortly.");
	});

	it("shows an empty-eligible message when the catalog is confirmed empty", () => {
		const w = mount(AgentModelPicker, {
			props: { eligible: { catalog: "ok", models: [] } },
		});
		expect(w.text()).toContain("No eligible models right now.");
	});
});
