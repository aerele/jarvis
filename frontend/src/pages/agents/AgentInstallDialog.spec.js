import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// setAgentModel stays mocked so "the dialog never calls it" is asserted: the pick
// rides install_agent (the parent's call), never a separate save. getAgentModel is
// the dialog's own fresh read of the row on open (RES7-1).
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

// `row`: what the dialog's own get_agent_model read returns on open.
function mountDialog({ row = null, ...props } = {}) {
	apiAgents.getAgentModel.mockResolvedValue(row);
	return mount(AgentInstallDialog, {
		props: {
			modelValue: true,
			agentSlug: "close-auditor",
			agentTitle: "Close Auditor",
			requiredTier: "Advanced",
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
}

const installBtn = (w) => w.find('[data-label="Install"]');

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
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
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

describe("AgentInstallDialog header close button (FE3-1)", () => {
	it("closes like Cancel", async () => {
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
		expect(w.text()).toContain("Model list unavailable. Try again shortly.");
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
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).not.toContain("Claude Opus 5");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
		expectNoSeparateSave();
	});

	it("Change away from the existing choice emits the new pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
		await flushPromises();
		await pickOther(w);

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "openai", model: "gpt-5" }]]);
		expectNoSeparateSave();
	});

	it("Change to the best eligible model is still a pick when a row holds another", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
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
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).toContain("$2.00 in / $6.00 out per 1M tokens");
	});

	it("no existing row falls back to the auto-pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ row: null });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
	});

	it("every pinned state with an unchanged choice emits no pick", async () => {
		for (const state of ["auto", "chosen", "changed"]) {
			apiAgents.getEligibleModels.mockResolvedValue({
				catalog: "ok",
				models: [OPUS, SONNET],
			});
			const w = mountDialog({ row: { state, choice: SONNET } });
			await flushPromises();
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
			row: { state: "chosen", choice: { provider: "", model: "kimi-sub" } },
		});
		await flushPromises();
		expect(w.text()).toContain("Kimi Sub");
		expect(w.text()).toContain("Uses your Kimi (Moonshot) subscription allowance");
		expect(w.text()).not.toContain("Claude Opus 5");

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
// shown model is sent even when unchanged - else the agent would not run it. It
// goes as ifUnpinned: the server keeps a row someone pinned since (RES7-1).
describe("AgentInstallDialog over an unpinned row (legacy / needs_model)", () => {
	it("legacy row + unchanged pick sends the shown model", async () => {
		mockTwoModels();
		const w = mountDialog({ row: { state: "legacy", choice: null } });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([
			[{ provider: "anthropic", model: "claude-opus-5", ifUnpinned: true }],
		]);
		expectNoSeparateSave();
	});

	it("needs_model row keeping a stale model shows and sends the best eligible instead", async () => {
		mockTwoModels();
		const w = mountDialog({ row: { state: "needs_model", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Claude Sonnet 5");

		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([
			[{ provider: "anthropic", model: "claude-opus-5", ifUnpinned: true }],
		]);
	});

	it("a changed pick over a legacy row is sent as picked", async () => {
		mockTwoModels();
		const w = mountDialog({ row: { state: "legacy", choice: null } });
		await flushPromises();
		await pickOther(w);
		await w.find('[data-label="Install"]').trigger("click");
		expect(w.emitted("confirm")).toEqual([[{ provider: "openai", model: "gpt-5" }]]);
	});
});

// RES7-1: the row is read fresh on every open, never taken from the page's copy.
describe("AgentInstallDialog reads the row fresh on open", () => {
	it("reads the row for this agent alongside the eligible models", async () => {
		mockTwoModels();
		mountDialog();
		await flushPromises();
		expect(apiAgents.getAgentModel).toHaveBeenCalledWith("close-auditor");
		expect(apiAgents.getEligibleModels).toHaveBeenCalledWith("close-auditor");
	});

	it("a pin set since the last open is shown and not overwritten", async () => {
		mockTwoModels();
		const w = mountDialog({ row: { state: "legacy", choice: null } });
		await flushPromises();
		await w.setProps({ modelValue: false });
		apiAgents.getAgentModel.mockResolvedValue({ state: "chosen", choice: SONNET });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		await installBtn(w).trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});

	it("a slower row read from a prior open never overwrites the newer one", async () => {
		mockTwoModels();
		let resolveFirst;
		apiAgents.getAgentModel
			.mockReturnValueOnce(new Promise((resolve) => (resolveFirst = resolve)))
			.mockResolvedValueOnce({ state: "chosen", choice: SONNET });
		const w = mount(AgentInstallDialog, {
			props: { modelValue: true, agentSlug: "close-auditor", agentTitle: "Close Auditor" },
		});
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		resolveFirst({ state: "legacy", choice: null });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		await installBtn(w).trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});
});

// RES7-2: a failed load is never an installable empty box.
describe("AgentInstallDialog failed load", () => {
	it("disables Install and offers Retry when the eligible models fail to load", async () => {
		apiAgents.getEligibleModels.mockRejectedValueOnce(new Error("boom"));
		const w = mountDialog();
		await flushPromises();
		expect(w.text()).toContain("Couldn't load the models this agent can use.");
		expect(installBtn(w).attributes("disabled")).toBeDefined();
		// FE8-a11y: the failure is also announced via the aria-live region.
		expect(w.find('[role="status"]').text()).toBe(
			"Couldn't load the models this agent can use."
		);
		await installBtn(w).trigger("click");
		expect(w.emitted("confirm")).toBeUndefined();

		mockTwoModels();
		await w
			.findAll("button")
			.find((b) => b.text() === "Retry")
			.trigger("click");
		await flushPromises();
		expect(apiAgents.getEligibleModels).toHaveBeenCalledTimes(2);
		expect(w.text()).toContain("Claude Opus 5");
		expect(installBtn(w).attributes("disabled")).toBeUndefined();
		// FE8-a11y: a successful reload clears the earlier failure announcement.
		expect(w.find('[role="status"]').text()).toBe("");
	});

	it("an empty response or a failed row read is a failed load too", async () => {
		for (const setup of [
			() => apiAgents.getEligibleModels.mockResolvedValue(null),
			() => {
				mockTwoModels();
				apiAgents.getAgentModel.mockRejectedValue(new Error("boom"));
			},
		]) {
			const w = mountDialog();
			setup();
			await w.setProps({ modelValue: false });
			await w.setProps({ modelValue: true });
			await flushPromises();
			expect(w.text()).toContain("Couldn't load the models this agent can use.");
			expect(installBtn(w).attributes("disabled")).toBeDefined();
			expect(w.find('[role="status"]').text()).toBe(
				"Couldn't load the models this agent can use."
			);
		}
	});
});

// RES7-3: an unreadable catalog does not block installing over a pinned row.
describe("AgentInstallDialog unreadable catalog over a pinned row", () => {
	it("keeps the pinned model, hides Change and installs with no pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "unknown", models: [] });
		const w = mountDialog({ row: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).toContain("Model list unavailable. Installing keeps this model.");
		expect(w.find('[data-label="Change"]').exists()).toBe(false);
		expect(installBtn(w).attributes("disabled")).toBeUndefined();
		await installBtn(w).trigger("click");
		expect(w.emitted("confirm")).toEqual([[]]);
	});

	it("still blocks Install over a legacy row (there is nothing to keep)", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "unknown", models: [] });
		const w = mountDialog({ row: { state: "legacy", choice: null } });
		await flushPromises();
		expect(w.text()).toContain("Model list unavailable. Try again shortly.");
		expect(installBtn(w).attributes("disabled")).toBeDefined();
	});
});

// UX7-1: the tenant-wide reach is always stated, not only when unchanged.
describe("AgentInstallDialog tenant-wide copy", () => {
	it("says the choice applies to everyone, before and after a Change", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		const line = "This choice applies to everyone using this agent.";
		expect(w.text()).toContain(line);
		await pickOther(w);
		expect(w.text()).toContain(line);
	});
});
