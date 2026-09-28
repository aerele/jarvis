import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const apiAgents = vi.hoisted(() => ({
	getEligibleModels: vi.fn(),
	setAgentModel: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn() },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="dialog"><div class="dialog-title">{{ options && options.title }}</div><slot name="body-content" /><slot name="actions" /></div>`,
	},
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
vi.mock("./AgentModelPicker.vue", () => ({
	default: {
		name: "AgentModelPicker",
		props: ["eligible", "loading", "current", "saving"],
		emits: ["select"],
		template: `<div class="picker" :data-saving="saving"><button data-testid="pick-other" @click="$emit('select', { provider: 'openai', model: 'gpt-5' })">pick other</button></div>`,
	},
}));
vi.mock("@/lib/errors", () => ({
	errHtml: (e) => (e && e.message) || String(e),
}));

import AgentInstallDialog from "./AgentInstallDialog.vue";

const OPUS = {
	provider: "anthropic",
	model: "claude-opus-5",
	label: "Claude Opus 5",
	capability_tier: "Frontier",
	cost_note: "$5.00 in / $15.00 out per 1M tokens",
};

function mountDialog(props = {}) {
	return mount(AgentInstallDialog, {
		props: {
			modelValue: true,
			agentSlug: "close-auditor",
			agentTitle: "Close Auditor",
			requiredTier: "Advanced",
			installing: false,
			...props,
		},
	});
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("AgentInstallDialog auto-pick", () => {
	it("shows the best eligible model as the auto-pick, with its tier + cost note", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({
			agent: "close-auditor",
			required_tier: "Advanced",
			catalog: "ok",
			models: [
				OPUS,
				{ ...OPUS, provider: "openai", model: "gpt-5", label: "GPT-5", cost_note: "" },
			],
		});
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain("Needs an Advanced model or better.");
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).toContain("$5.00 in / $15.00 out per 1M tokens");
		const tierBadge = w.findAll(".badge").find((b) => b.text() === "Frontier");
		expect(tierBadge).toBeTruthy();
	});

	it("changing the pick calls set_agent_model and updates the displayed model", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({
			catalog: "ok",
			models: [
				OPUS,
				{
					provider: "openai",
					model: "gpt-5",
					label: "GPT-5",
					capability_tier: "Advanced",
					cost_note: "",
				},
			],
		});
		apiAgents.setAgentModel.mockResolvedValue({});
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledWith("close-auditor", "openai", "gpt-5");
		expect(w.text()).toContain("GPT-5");
	});

	it("emits confirm when Install is clicked", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toHaveLength(1);
	});
});

describe("AgentInstallDialog double-submit guard", () => {
	it("two rapid picks fire set_agent_model exactly once", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({
			catalog: "ok",
			models: [
				OPUS,
				{
					provider: "openai",
					model: "gpt-5",
					label: "GPT-5",
					capability_tier: "Advanced",
					cost_note: "",
				},
			],
		});
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");

		const pick = w.find('[data-testid="pick-other"]');
		await pick.trigger("click");
		await pick.trigger("click");
		await pick.trigger("click");

		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.find(".picker").attributes("data-saving")).toBe("true");

		resolveSet({});
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
	});
});

describe("AgentInstallDialog none-eligible / unknown catalog", () => {
	it("disables Install and explains when no model is eligible", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [] });
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain(
			"None of your connected AI providers offer a model this agent needs."
		);
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();
	});

	it("none eligible: an admin gets a Connect-a-provider CTA", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [] });
		const w = mountDialog({ isAdmin: true });
		await flushPromises();
		const cta = w.findAll("button").find((b) => b.text() === "Connect a provider");
		expect(cta).toBeTruthy();
		await cta.trigger("click");
		expect(w.emitted("connect-provider")).toHaveLength(1);
	});

	it("none eligible: a plain user gets no CTA, just plain copy", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [] });
		const w = mountDialog({ isAdmin: false });
		await flushPromises();
		expect(w.findAll("button").some((b) => b.text() === "Connect a provider")).toBe(false);
		expect(w.text()).toContain("Ask an admin to connect a provider that offers one.");
	});

	it("disables Install and shows the try-again copy on an unreadable catalog", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "unknown", models: [] });
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain("Model list unavailable — try again shortly.");
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();
	});
});

describe("AgentInstallDialog open/close lifecycle", () => {
	it("re-fetches and resets the pick every time it opens", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		const w = mount(AgentInstallDialog, {
			props: {
				modelValue: false,
				agentSlug: "close-auditor",
				agentTitle: "Close Auditor",
				requiredTier: "Advanced",
			},
		});
		expect(apiAgents.getEligibleModels).not.toHaveBeenCalled();
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(apiAgents.getEligibleModels).toHaveBeenCalledTimes(1);
	});
});
