import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// setAgentModel/getAgentModel stay mocked so "the dialog never calls them" is
// asserted: the pick rides install_agent (the parent's call), never a separate save.
const apiAgents = vi.hoisted(() => ({
	getEligibleModels: vi.fn(),
	setAgentModel: vi.fn(),
	getAgentModel: vi.fn(),
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
		props: ["label", "icon", "disabled", "loading", "variant"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-loading="loading" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="dialog"><slot name="body-header"><div class="dialog-title">{{ options && options.title }}</div></slot><slot name="body-content" /><slot name="actions" /></div>`,
	},
}));
// FE3-1: the #body-header override renders a real DialogTitle, which needs a
// DialogRoot ancestor the Dialog mock above isn't (see SettingsDialog.spec.js).
vi.mock("reka-ui", () => ({
	DialogTitle: { name: "DialogTitle", template: `<span><slot /></span>` },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
vi.mock("./AgentModelPicker.vue", () => ({
	default: {
		name: "AgentModelPicker",
		props: ["eligible", "loading", "current", "saving"],
		emits: ["select"],
		template: `<div class="picker" :data-saving="saving">
			<button data-testid="pick-other" @click="$emit('select', { provider: 'openai', model: 'gpt-5' })">pick other</button>
			<button data-testid="pick-opus" @click="$emit('select', { provider: 'anthropic', model: 'claude-opus-5' })">pick opus</button>
			<button data-testid="pick-kimi" @click="$emit('select', { provider: '', model: 'kimi-sub' })">pick kimi</button>
		</div>`,
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

const GPT5 = {
	provider: "openai",
	model: "gpt-5",
	label: "GPT-5",
	capability_tier: "Advanced",
	cost_note: "",
};

// A subscription-lane model: provider is "" by design.
const KIMI = {
	provider: "",
	model: "kimi-sub",
	label: "Kimi Sub",
	capability_tier: "Advanced",
	cost_note: "Uses your Kimi (Moonshot) subscription allowance",
};

// A non-top existing tenant-wide pick (EDGE2-1), distinct from OPUS and GPT5.
const SONNET = {
	provider: "anthropic",
	model: "claude-sonnet-5",
	label: "Claude Sonnet 5",
	capability_tier: "Advanced",
	cost_note: "$2.00 in / $6.00 out per 1M tokens",
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

function mockTwoModels() {
	apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, GPT5] });
}

async function pickOther(w) {
	await w.find('[data-label="Change"]').trigger("click");
	await w.find('[data-testid="pick-other"]').trigger("click");
	await flushPromises();
}

function expectNoSeparateSave() {
	expect(apiAgents.setAgentModel).not.toHaveBeenCalled();
	expect(apiAgents.getAgentModel).not.toHaveBeenCalled();
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
			models: [OPUS, { ...GPT5, capability_tier: "Frontier" }],
		});
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain("Needs an Advanced model or better.");
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).toContain("$5.00 in / $15.00 out per 1M tokens");
		const tierBadge = w.findAll(".badge").find((b) => b.text() === "Frontier");
		expect(tierBadge).toBeTruthy();
	});

	it("Install is disabled while the eligible models load", async () => {
		apiAgents.getEligibleModels.mockReturnValue(new Promise(() => {}));
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain("Loading eligible models");
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();
	});
});

describe("AgentInstallDialog confirm carries the pick", () => {
	it("an untouched auto-pick emits confirm with no pick", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
		expectNoSeparateSave();
	});

	it("a changed pick is emitted with confirm", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await pickOther(w);
		expect(w.text()).toContain("GPT-5");
		expect(w.find('[role="status"]').text()).toBe("Model selected.");
		expect(w.emitted("confirm")).toBeUndefined();

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "openai", model: "gpt-5" }]]);
		expectNoSeparateSave();
	});

	it("picking back to the shown model emits no pick", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-opus"]').trigger("click");
		await flushPromises();

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});

	it("Cancel after a changed pick closes without confirm or any network call", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Cancel"]').trigger("click");
		await flushPromises();
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
		expect(w.emitted("confirm")).toBeUndefined();
		expectNoSeparateSave();
	});

	it("never calls setAgentModel across pick, install, close and reopen", async () => {
		mockTwoModels();
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Install"]').trigger("click");
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(w.emitted("confirm")).toHaveLength(2);
		expectNoSeparateSave();
	});
});

// The parent's install_agent is in flight: nothing in the dialog may close it or
// send a second confirm.
describe("AgentInstallDialog while the parent is installing", () => {
	it("Install shows loading and is disabled; Change, Cancel and the header X are disabled", async () => {
		mockTwoModels();
		const w = mountDialog({ installing: true });
		await flushPromises();
		const install = w.find('[data-label="Install"]');
		expect(install.attributes("data-loading")).toBe("true");
		expect(install.attributes("disabled")).toBeDefined();
		for (const label of ["Change", "Cancel", "Close"]) {
			expect(w.find(`[data-label="${label}"]`).attributes("disabled")).toBeDefined();
		}
	});

	it("Escape/backdrop (the Dialog's own update:modelValue) is ignored", async () => {
		mockTwoModels();
		const w = mountDialog({ installing: true });
		await flushPromises();
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});

	it("controls re-enable once installing clears", async () => {
		mockTwoModels();
		const w = mountDialog({ installing: true });
		await flushPromises();
		await w.setProps({ installing: false });
		for (const label of ["Install", "Change", "Cancel", "Close"]) {
			expect(w.find(`[data-label="${label}"]`).attributes("disabled")).toBeUndefined();
		}
	});
});

describe("AgentInstallDialog header close button (FE3-1)", () => {
	it("closes like Cancel when not installing", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeUndefined();
		await w.find('[data-label="Close"]').trigger("click");
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
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
		const w = mountDialog({ modelValue: false });
		expect(apiAgents.getEligibleModels).not.toHaveBeenCalled();
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(apiAgents.getEligibleModels).toHaveBeenCalledTimes(1);
	});

	// UX-4: a draft from a prior open must never be resurrected.
	it("a draft from a prior open never survives into the next open", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await pickOther(w);
		expect(w.text()).toContain("GPT-5");

		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("GPT-5");
		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});

	it("a slower load from a prior open never overwrites the newer one", async () => {
		let resolveFirst;
		apiAgents.getEligibleModels
			.mockReturnValueOnce(new Promise((resolve) => (resolveFirst = resolve)))
			.mockResolvedValueOnce({ catalog: "ok", models: [OPUS, GPT5] });
		const w = mountDialog();
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		await pickOther(w);

		resolveFirst({ catalog: "ok", models: [SONNET] });
		await flushPromises();
		expect(w.text()).toContain("GPT-5");
		expect(w.text()).not.toContain("Claude Sonnet 5");
	});
});

// EDGE2-1: an existing tenant-wide row is what runs unless changed, so the
// dialog shows it rather than recomputing the best eligible model.
describe("AgentInstallDialog seeds from an existing tenant-wide choice (EDGE2-1)", () => {
	it("shows the existing chosen model; Install without Change emits no pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).not.toContain("Claude Opus 5");
		expect(w.text()).toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
		expectNoSeparateSave();
	});

	it("Change away from the existing choice emits the new pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		await pickOther(w);
		expect(w.text()).not.toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "openai", model: "gpt-5" }]]);
		expectNoSeparateSave();
	});

	it("Change to the best eligible model is still a pick when a row holds another", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-opus"]').trigger("click");
		await flushPromises();

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([
			[{ provider: "anthropic", model: "claude-opus-5" }],
		]);
	});

	it("an existing choice that has fallen out of the eligible list is still shown by its own label", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).toContain("$2.00 in / $6.00 out per 1M tokens");
	});

	it("no existing row falls back to the auto-pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: null });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Already set for everyone using this agent.");
	});

	it("every pinned state with an unchanged choice emits no pick", async () => {
		for (const state of ["auto", "chosen", "changed"]) {
			apiAgents.getEligibleModels.mockResolvedValue({
				catalog: "ok",
				models: [OPUS, SONNET],
			});
			const w = mountDialog({ modelInfo: { state, choice: SONNET } });
			await flushPromises();
			expect(w.text()).toContain("Already set for everyone using this agent.");
			await w.find('[data-label="Install"]').trigger("click");
			expect(w.emitted("confirm")).toEqual([[]]);
		}
	});
});

// A subscription-lane choice has provider "" - it is still an existing choice.
describe('AgentInstallDialog subscription-lane models (provider "")', () => {
	it("seeds from an existing subscription-lane choice and matches it in the eligible list", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, KIMI] });
		const w = mountDialog({
			modelInfo: { state: "chosen", choice: { provider: "", model: "kimi-sub" } },
		});
		await flushPromises();
		expect(w.text()).toContain("Kimi Sub");
		expect(w.text()).toContain("Uses your Kimi (Moonshot) subscription allowance");
		expect(w.text()).not.toContain("Claude Opus 5");
		expect(w.text()).toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});

	it("a subscription-lane pick is emitted with its empty provider kept", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, KIMI] });
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-kimi"]').trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Kimi Sub");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "", model: "kimi-sub" }]]);
	});
});

// install_agent keeps a legacy/needs_model row as-is without a pick, so the
// shown model is sent even when unchanged - else the agent would not run it.
describe("AgentInstallDialog over an unpinned row (legacy / needs_model)", () => {
	it("legacy row + unchanged pick sends the shown model", async () => {
		mockTwoModels();
		const w = mountDialog({ modelInfo: { state: "legacy", choice: null } });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([
			[{ provider: "anthropic", model: "claude-opus-5" }],
		]);
		expectNoSeparateSave();
	});

	it("needs_model row keeping a stale model shows and sends the best eligible instead", async () => {
		mockTwoModels();
		const w = mountDialog({ modelInfo: { state: "needs_model", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Claude Sonnet 5");
		expect(w.text()).not.toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([
			[{ provider: "anthropic", model: "claude-opus-5" }],
		]);
	});

	it("a changed pick over a legacy row is sent as picked", async () => {
		mockTwoModels();
		const w = mountDialog({ modelInfo: { state: "legacy", choice: null } });
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "openai", model: "gpt-5" }]]);
	});
});
