import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const apiAgents = vi.hoisted(() => ({
	getAgentModel: vi.fn(),
	getEligibleModels: vi.fn(),
	setAgentModel: vi.fn(),
	resetAgentModel: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
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
		template: `<div v-if="modelValue" class="dialog"><div class="dialog-title">{{ options && options.title }}</div><slot name="body-content" /></div>`,
	},
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
vi.mock("@/components/Banner.vue", () => ({
	default: {
		name: "Banner",
		props: ["type", "message"],
		template: `<div class="banner" :data-type="type">{{ message }}<slot name="action" /></div>`,
	},
}));
vi.mock("./AgentModelPicker.vue", () => ({
	default: {
		name: "AgentModelPicker",
		props: ["eligible", "loading", "current", "saving"],
		emits: ["select"],
		template: `<div class="picker" :data-saving="saving"><button data-testid="pick" @click="$emit('select', { provider: 'openai', model: 'gpt-5' })">pick</button></div>`,
	},
}));
vi.mock("@/lib/errors", () => ({
	errMessage: (e) => (e && e.message) || String(e),
	errHtml: (e) => (e && e.message) || String(e),
}));

import AgentModelCard from "./AgentModelCard.vue";

function elig(models, catalog = "ok") {
	return { agent: "close-auditor", required_tier: "Advanced", catalog, models };
}
const OPUS = {
	provider: "anthropic",
	model: "claude-opus-5",
	label: "Claude Opus 5",
	capability_tier: "Frontier",
	cost_note: "",
};

function mountCard(props = {}) {
	return mount(AgentModelCard, { props: { agentSlug: "close-auditor", ...props } });
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("AgentModelCard flag-off / ungoverned guard", () => {
	it("renders nothing when enforce_agent_min_model is off", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "close-auditor",
			enforced: 0,
			min_model: null,
			state: null,
		});
		const w = mountCard();
		await flushPromises();
		expect(w.html()).toBe("<!--v-if-->");
		expect(apiAgents.getEligibleModels).not.toHaveBeenCalled();
	});

	it("renders nothing when the flag is on but nothing governs this agent yet", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "close-auditor",
			enforced: 1,
			min_model: null,
			state: null,
		});
		const w = mountCard();
		await flushPromises();
		expect(w.find("section").exists()).toBe(false);
	});

	// T6 round 2: this is exactly what AgentDetail hands down once its OWN
	// probe has settled off (modelInfoSettled) - seeded, so the card never
	// fetches, and at NO tick (sync mount, or after a flush) does it render
	// anything, matching the "flag-off must stay zero-DOM" requirement.
	it("flag-off, seeded from the parent's settled probe: zero fetches, zero DOM at any tick", async () => {
		const w = mountCard({
			initialInfo: { agent: "close-auditor", enforced: 0, min_model: null, state: null },
		});
		expect(w.html()).toBe("<!--v-if-->"); // synchronously, before any flush
		await flushPromises();
		expect(w.html()).toBe("<!--v-if-->"); // still, after settling
		expect(apiAgents.getAgentModel).not.toHaveBeenCalled();
		expect(apiAgents.getEligibleModels).not.toHaveBeenCalled();
	});
});

describe("AgentModelCard dedupe: reuses the parent's already-loaded probe", () => {
	const SEEDED_INFO = {
		agent: "a",
		enforced: 1,
		min_model: { tier: "Advanced" },
		required_tier: "Advanced",
		state: "chosen",
		choice: {
			provider: "anthropic",
			model: "claude-opus-5",
			label: "Claude Opus 5",
			capability_tier: "Frontier",
		},
		pending_apply: 0,
	};

	it("renders straight from initialInfo/initialEligible, no fetch on mount", async () => {
		const w = mountCard({ initialInfo: SEEDED_INFO, initialEligible: elig([OPUS]) });
		await flushPromises();
		expect(apiAgents.getAgentModel).not.toHaveBeenCalled();
		expect(apiAgents.getEligibleModels).not.toHaveBeenCalled();
		expect(w.text()).toContain("Claude Opus 5");
	});

	it("falls back to its own fetch when the parent hands down nothing yet", async () => {
		apiAgents.getAgentModel.mockResolvedValue(SEEDED_INFO);
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard(); // no initialInfo
		await flushPromises();
		expect(apiAgents.getAgentModel).toHaveBeenCalledWith("close-auditor");
		expect(w.text()).toContain("Claude Opus 5");
	});
});

describe("AgentModelCard loading / error states", () => {
	it("renders NOTHING while its own fallback fetch is first in flight (flag-off must stay zero-DOM)", () => {
		apiAgents.getAgentModel.mockReturnValue(new Promise(() => {})); // never resolves
		const w = mountCard();
		expect(w.html()).toBe("<!--v-if-->");
		expect(w.text()).not.toContain("Loading…");
	});

	it("shows the error inline with Retry, and Retry re-fetches", async () => {
		apiAgents.getAgentModel.mockRejectedValueOnce(new Error("network down"));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("network down");
		const retry = w.find('[data-label="Retry"]');
		expect(retry.exists()).toBe(true);

		apiAgents.getAgentModel.mockResolvedValueOnce({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "legacy",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValueOnce(elig([OPUS]));
		await retry.trigger("click");
		await flushPromises();
		expect(w.text()).not.toContain("network down");
		expect(w.text()).toContain("Using your default model");
	});

	it("Retry stays visible (swaps to Loading…) instead of vanishing mid-click", async () => {
		apiAgents.getAgentModel.mockRejectedValueOnce(new Error("network down"));
		const w = mountCard();
		await flushPromises();
		expect(w.find('[data-label="Retry"]').exists()).toBe(true);

		apiAgents.getAgentModel.mockReturnValue(new Promise(() => {})); // retry never resolves
		await w.find('[data-label="Retry"]').trigger("click");
		// still visible RIGHT AFTER the click, before the retry settles
		expect(w.text()).toContain("Loading…");
		expect(w.html()).not.toBe("<!--v-if-->");
	});
});

describe("AgentModelCard states", () => {
	it("legacy: banner + 'Your default model'", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: null,
			required_tier: null,
			state: "legacy",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("Using your default model");
		expect(w.text()).toContain("Your default model");
	});

	it("chosen: shows the pinned model + its capability tier", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		const tierBadge = w.findAll(".badge").find((b) => b.text() === "Frontier");
		expect(tierBadge).toBeTruthy();
		expect(w.text()).not.toContain("Pending apply");
	});

	it("pending apply: shows the badge when chosen != pushed", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 1,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("Pending apply");
	});

	it("changed: names the model it moved to", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "changed",
			choice: {
				provider: "openai",
				model: "gpt-5",
				label: "GPT-5",
				capability_tier: "Advanced",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("this agent moved to GPT-5");
	});

	it("needs_model: paused copy + Connect-a-provider CTA for an admin", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "needs_model",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([], "ok"));
		const w = mountCard({ isAdmin: true });
		await flushPromises();
		expect(w.text()).toContain("This agent is paused");
		expect(w.find('[data-label="Connect a provider"]').exists()).toBe(true);
	});

	it("needs_model: no Connect-a-provider button for a plain user", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "needs_model",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([], "ok"));
		const w = mountCard({ isAdmin: false });
		await flushPromises();
		expect(w.find('[data-label="Connect a provider"]').exists()).toBe(false);
	});

	// Minor review fix: nothing runs in needs_model (paused) - "Your default
	// model" implies a model is actually in use, which is not true here.
	it("needs_model: the current-model label reads 'No model selected', not 'Your default model'", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "needs_model",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([], "ok"));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("No model selected");
		expect(w.text()).not.toContain("Your default model");
	});

	it("unknown catalog: shows the try-again copy, never claims none eligible", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "unknown", models: [] });
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain("Model list unavailable — try again shortly.");
		expect(w.text()).not.toContain("None of your connected AI providers");
	});

	it("none eligible: Unavailable badge + copy, Change disabled", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "needs_model",
			choice: null,
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([], "ok"));
		const w = mountCard();
		await flushPromises();
		expect(w.text()).toContain(
			"None of your connected AI providers offer a model this agent needs."
		);
		expect(w.find('[data-label="Change"]').attributes("disabled")).toBeDefined();
	});
});

describe("UX-3 review fix: a persistent explanation for the 'Pending apply' badge", () => {
	function pendingInfo(overrides = {}) {
		return {
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 1,
			...overrides,
		};
	}

	it("survives a reload (no just-saved event): a plain user is told to ask an admin", async () => {
		apiAgents.getAgentModel.mockResolvedValue(pendingInfo());
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ canApply: false });
		await flushPromises();
		expect(w.text()).toContain("Pending apply");
		expect(w.text()).toContain("Saved. An admin must apply changes before it takes effect.");
	});

	it("survives a reload: a reviewer (canApply) is told to apply it themselves", async () => {
		apiAgents.getAgentModel.mockResolvedValue(pendingInfo());
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ canApply: true });
		await flushPromises();
		expect(w.text()).toContain("Saved — apply catalog changes on the Agents page to use it.");
	});

	it("says nothing when nothing is pending", async () => {
		apiAgents.getAgentModel.mockResolvedValue(pendingInfo({ pending_apply: 0 }));
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ canApply: false });
		await flushPromises();
		expect(w.text()).not.toContain("An admin must apply changes");
	});

	it("does not duplicate the sentence while the just-saved Banner is already showing it", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(
			elig([OPUS, { ...OPUS, provider: "openai", model: "gpt-5", label: "GPT-5" }])
		);
		apiAgents.setAgentModel.mockResolvedValue(pendingInfo());
		const w = mountCard({ canApply: false });
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await flushPromises();
		await w.find('[data-testid="pick"]').trigger("click");
		await flushPromises();

		// The Banner AND the sr-only aria-live announcement (unrelated to this
		// fix - liveMessage already mirrors the Banner's own text) both carry
		// this sentence, so 2 is the correct baseline; the persistent
		// pendingApplyText line must not add a THIRD.
		const sentence = "Saved. An admin must apply changes before it takes effect.";
		const occurrences = w.text().split(sentence).length - 1;
		expect(occurrences).toBe(2);
	});
});

describe("AgentModelCard save flow", () => {
	async function openAndPick(props) {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(
			elig([OPUS, { ...OPUS, provider: "openai", model: "gpt-5", label: "GPT-5" }])
		);
		apiAgents.setAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "openai",
				model: "gpt-5",
				label: "GPT-5",
				capability_tier: "Advanced",
			},
			pending_apply: 1,
		});
		const w = mountCard(props);
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await flushPromises();
		await w.find('[data-testid="pick"]').trigger("click");
		await flushPromises();
		return w;
	}

	it("calls set_agent_model with the exact {provider, model} the picker emitted", async () => {
		await openAndPick();
		expect(apiAgents.setAgentModel).toHaveBeenCalledWith("close-auditor", "openai", "gpt-5");
	});

	it("reviewer (canApply) sees the apply-it-yourself copy", async () => {
		const w = await openAndPick({ canApply: true });
		expect(w.text()).toContain("Saved — apply catalog changes on the Agents page to use it.");
	});

	it("plain user (canApply=false) sees the ask-an-admin copy", async () => {
		const w = await openAndPick({ canApply: false });
		expect(w.text()).toContain("Saved. An admin must apply changes before it takes effect.");
	});
});

describe("AgentModelCard double-submit guard", () => {
	it("two rapid picks fire set_agent_model exactly once", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountCard();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await flushPromises();

		const pick = w.find('[data-testid="pick"]');
		await pick.trigger("click"); // fires the in-flight request
		await pick.trigger("click"); // must be a no-op: saving is already true
		await pick.trigger("click");

		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.find(".picker").attributes("data-saving")).toBe("true");

		resolveSet({
			agent: "a",
			enforced: 1,
			state: "chosen",
			choice: {
				provider: "openai",
				model: "gpt-5",
				label: "GPT-5",
				capability_tier: "Advanced",
			},
			pending_apply: 1,
		});
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
	});
});

describe("AgentModelCard note banner - canApply-aware, never the raw backend note", () => {
	function withNote(overrides = {}) {
		return {
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
			fleet_unresolved: 1,
			note: "Apply catalog changes to finish updating this agent",
			...overrides,
		};
	}

	it("a reviewer (canApply) is offered an Open Agents page action, never the raw note", async () => {
		apiAgents.getAgentModel.mockResolvedValue(withNote());
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ canApply: true });
		await flushPromises();
		expect(w.text()).toContain("This agent's model setup needs to be applied");
		expect(w.text()).not.toContain("finish updating this agent");
		expect(w.find('[data-label="Open Agents page"]').exists()).toBe(true);
	});

	it("a plain user (canApply=false) is told to ask an admin, never the raw note", async () => {
		apiAgents.getAgentModel.mockResolvedValue(withNote());
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ canApply: false });
		await flushPromises();
		expect(w.text()).toContain("An admin needs to apply the latest agent changes.");
		expect(w.text()).not.toContain("finish updating this agent");
	});
});

describe("AgentModelCard reset (admins only)", () => {
	it("is hidden for a non-admin", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		const w = mountCard({ isAdmin: false });
		await flushPromises();
		expect(w.find('[data-label="Use automatic choice"]').exists()).toBe(false);
	});

	it("calls reset_agent_model for an admin", async () => {
		apiAgents.getAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "chosen",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 0,
		});
		apiAgents.getEligibleModels.mockResolvedValue(elig([OPUS]));
		apiAgents.resetAgentModel.mockResolvedValue({
			agent: "a",
			enforced: 1,
			min_model: { tier: "Advanced" },
			required_tier: "Advanced",
			state: "auto",
			choice: {
				provider: "anthropic",
				model: "claude-opus-5",
				label: "Claude Opus 5",
				capability_tier: "Frontier",
			},
			pending_apply: 1,
		});
		const w = mountCard({ isAdmin: true });
		await flushPromises();
		await w.find('[data-label="Use automatic choice"]').trigger("click");
		await flushPromises();
		expect(apiAgents.resetAgentModel).toHaveBeenCalledWith("close-auditor");
	});
});
