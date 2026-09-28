import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

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
// FE3-1: AgentInstallDialog's #body-header override renders a real DialogTitle
// (see SettingsDialog.spec.js for the same mock - injectDialogRootContext()
// throws without a real DialogRoot ancestor, which the Dialog mock above isn't).
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
		// "pick-third" is a distinct value from both the auto-pick (OPUS) and
		// "pick-other" (openai/gpt-5) - needed for UX4-1's different-pick-retry
		// tests, which need to tell a stale pick apart from a new one.
		template: `<div class="picker" :data-saving="saving">
			<button data-testid="pick-other" @click="$emit('select', { provider: 'openai', model: 'gpt-5' })">pick other</button>
			<button data-testid="pick-third" @click="$emit('select', { provider: 'anthropic', model: 'claude-haiku-5' })">pick third</button>
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

// A non-top existing tenant-wide pick (EDGE2-1) - distinct from OPUS above AND
// from openai/gpt-5, which the AgentModelPicker mock's "pick other" button
// always emits, so a Change-away test can tell the two apart.
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

	it("emits confirm when Install is clicked with the auto-pick untouched (no set_agent_model call)", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(w.emitted("confirm")).toHaveLength(1);
		expect(apiAgents.setAgentModel).not.toHaveBeenCalled();
	});
});

// UX-4 review fix: picking is now a LOCAL draft, not an immediate
// set_agent_model call - the row must not change until Install is actually
// confirmed. (Modified from the pre-fix version of this file, which asserted
// the OPPOSITE - that picking called set_agent_model right away; that was
// exactly the bug: Cancel after a pick used to leave a tenant-wide model
// change behind for an agent that was never installed.)
describe("AgentInstallDialog local draft (UX-4)", () => {
	function mockTwoModels() {
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
	}

	it("picking a different model updates the display but does NOT call set_agent_model", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		expect(w.text()).toContain("GPT-5");
		expect(apiAgents.setAgentModel).not.toHaveBeenCalled();
	});

	it("Cancel after picking a different model discards the draft - no network call ever happens", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		await w.find('[data-label="Cancel"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).not.toHaveBeenCalled();
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("Install sends the changed draft first, then emits confirm only once it succeeds", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockResolvedValue({});
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		expect(w.emitted("confirm")).toBeUndefined();

		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledWith("close-auditor", "openai", "gpt-5");
		expect(w.emitted("confirm")).toHaveLength(1);
	});

	it("a failed set_agent_model surfaces the error and does NOT emit confirm / install", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockRejectedValue(new Error("model taken"));
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();

		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.emitted("confirm")).toBeUndefined();
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledWith("model taken");
	});
});

describe("AgentInstallDialog double-submit guard", () => {
	// UX-4: the guarded call moved from onPick (now a synchronous local
	// update, no network) to the Install-confirm's set_agent_model send -
	// this now exercises rapid Install clicks after a changed pick, not rapid
	// picks.
	it("two rapid Install clicks after a changed pick fire set_agent_model exactly once", async () => {
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
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();

		const install = w.find('[data-label="Install"]');
		await install.trigger("click"); // fires the in-flight request
		await install.trigger("click"); // must be a no-op: saving is already true
		await install.trigger("click");

		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.emitted("confirm")).toBeUndefined();

		resolveSet({});
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.emitted("confirm")).toHaveLength(1);
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

	// UX-4: a draft picked in a PRIOR open must never survive into the next
	// open (would otherwise silently resurrect a discarded pick).
	it("a draft from a prior open never survives into the next open", async () => {
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
		const w = mountDialog();
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		expect(w.text()).toContain("GPT-5");

		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("GPT-5");
	});
});

// FE2-1 review fix: confirmReqId used to be bumped only inside confirmInstall,
// so Cancel/Escape/backdrop (or a reopen) while setAgentModel was in flight
// could not stop a stale success from emitting confirm - the agent installed
// after the user had already cancelled. Cancel is now disabled and
// onClose/reopen become no-ops/invalidations while saving.
describe("AgentInstallDialog cancel-during-save guard (FE2-1)", () => {
	function mockTwoModels() {
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
	}
	async function startChangedInstall(w) {
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click");
		await flushPromises();
		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
	}

	it("Cancel is disabled while a save is in flight", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // never resolves
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		expect(w.find('[data-label="Cancel"]').attributes("disabled")).toBeDefined();
	});

	it("Escape/backdrop (Dialog's own update:modelValue) is ignored while saving; once the save resolves, confirm still fires", async () => {
		mockTwoModels();
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);

		// Simulate the Dialog component itself trying to close (Escape/backdrop) -
		// same path Cancel's onClose(false) uses.
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(w.emitted("update:modelValue")).toBeUndefined(); // swallowed, not forwarded to the parent

		resolveSet({});
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.emitted("confirm")).toHaveLength(1);
	});

	it("reopening while a prior confirmInstall request is in flight makes its stale resolution emit nothing", async () => {
		mockTwoModels();
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w); // 1st open's setAgentModel now pending

		// Forced close+reopen past the saving guard (e.g. an external reset) -
		// same component instance, modelValue toggled directly rather than via
		// onClose.
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();

		resolveSet({}); // the FIRST open's request finally resolves
		await flushPromises();
		expect(w.emitted("confirm")).toBeUndefined();
		const { toast } = await import("frappe-ui");
		expect(toast.error).not.toHaveBeenCalled();
	});
});

// EDGE2-1 review fix: the dialog used to always recompute its auto-pick from
// get_eligible_models, even when a tenant-wide `Jarvis Agent Model Choice` row
// already existed (e.g. another installer pinned a non-top model) - plan_install
// never overwrites that row, so the dialog showed a different model/tier/cost
// than what would actually run.
describe("AgentInstallDialog seeds from an existing tenant-wide choice (EDGE2-1)", () => {
	it("shows the existing chosen model (not the best-eligible one); Install without Change sends nothing", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).not.toContain("Claude Opus 5");
		expect(w.text()).toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).not.toHaveBeenCalled();
		expect(w.emitted("confirm")).toHaveLength(1);
	});

	it("Change away from the existing choice sends setAgentModel with the new pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		apiAgents.setAgentModel.mockResolvedValue({});
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		await w.find('[data-label="Change"]').trigger("click");
		await w.find('[data-testid="pick-other"]').trigger("click"); // emits openai/gpt-5
		await flushPromises();
		expect(w.text()).not.toContain("Already set for everyone using this agent.");

		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();
		expect(apiAgents.setAgentModel).toHaveBeenCalledWith("close-auditor", "openai", "gpt-5");
		expect(w.emitted("confirm")).toHaveLength(1);
	});

	it("an existing choice that has fallen out of the eligible list is still shown by its own label", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] }); // SONNET no longer offered
		const w = mountDialog({ modelInfo: { state: "chosen", choice: SONNET } });
		await flushPromises();
		expect(w.text()).toContain("Claude Sonnet 5");
		expect(w.text()).toContain("$2.00 in / $6.00 out per 1M tokens");
	});

	it("needs_model state (row exists but no choice) keeps today's auto-pick behaviour", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS] });
		const w = mountDialog({ modelInfo: { state: "needs_model", choice: null } });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Already set for everyone using this agent.");
	});

	it("no existing row falls back to today's auto-pick", async () => {
		apiAgents.getEligibleModels.mockResolvedValue({ catalog: "ok", models: [OPUS, SONNET] });
		const w = mountDialog({ modelInfo: null });
		await flushPromises();
		expect(w.text()).toContain("Claude Opus 5");
		expect(w.text()).not.toContain("Already set for everyone using this agent.");
	});
});

function mockTwoModels() {
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
}

async function startChangedInstall(w) {
	await w.find('[data-label="Change"]').trigger("click");
	await w.find('[data-testid="pick-other"]').trigger("click");
	await flushPromises();
	await w.find('[data-label="Install"]').trigger("click");
	await flushPromises();
}

// FE3-1 review fix: the default Dialog header X closes unconditionally, so
// while saving it was inert (onClose ignores it) but gave no disabled signal,
// unlike Cancel. The #body-header override's own close button must match
// Cancel's gating exactly.
describe("AgentInstallDialog header close button gated like Cancel (FE3-1)", () => {
	it("is enabled before a save starts", async () => {
		mockTwoModels();
		const w = mountDialog();
		await flushPromises();
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeUndefined();
	});

	it("is disabled while a save is in flight, and re-enables once it settles", async () => {
		mockTwoModels();
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeDefined();

		resolveSet({});
		await flushPromises();
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeUndefined();
	});

	it("a click while disabled is a no-op, exactly like Cancel", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // never resolves
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await w.find('[data-label="Close"]').trigger("click");
		await flushPromises();
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});
});

// FE3-2 review fix: confirmInstall used to await setAgentModel with no
// client-side bound - a stalled request locked Cancel disabled and the close
// icon inert forever. A ~15s ceiling now releases the dialog either way.
describe("AgentInstallDialog confirmInstall client-side timeout (FE3-2)", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("a stalled setAgentModel times out: honest error toast, saving clears, no confirm", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // never resolves
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		expect(w.find('[data-label="Cancel"]').attributes("disabled")).toBeDefined();

		await vi.advanceTimersByTimeAsync(15000);
		const { toast } = await import("frappe-ui");
		// UX4-1: the request is NOT actually cancelled (no AbortSignal to do it
		// with), so the copy no longer claims otherwise.
		expect(toast.error).toHaveBeenCalledWith(
			"Still saving your model choice. Wait a moment, then try again."
		);
		expect(w.find('[data-label="Cancel"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeUndefined();
		expect(w.emitted("confirm")).toBeUndefined();
	});

	it("a late resolution after the timeout never emits confirm or a second toast", async () => {
		mockTwoModels();
		let resolveSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveSet = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);

		await vi.advanceTimersByTimeAsync(15000);
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledTimes(1);

		resolveSet({}); // the stalled call finally settles, well past the timeout
		await flushPromises();
		expect(w.emitted("confirm")).toBeUndefined();
		expect(toast.error).toHaveBeenCalledTimes(1);
	});

	it("a late REJECTION after the timeout never surfaces a second toast either", async () => {
		mockTwoModels();
		let rejectSet;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((_resolve, reject) => {
				rejectSet = reject;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);

		await vi.advanceTimersByTimeAsync(15000);
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledTimes(1);

		rejectSet(new Error("model taken"));
		await flushPromises();
		expect(w.emitted("confirm")).toBeUndefined();
		expect(toast.error).toHaveBeenCalledTimes(1);
	});

	it("a request that settles just under the deadline is unaffected", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockResolvedValue({});
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await flushPromises();
		expect(w.emitted("confirm")).toHaveLength(1);

		const { toast } = await import("frappe-ui");
		await vi.advanceTimersByTimeAsync(15000);
		expect(toast.error).not.toHaveBeenCalled();
	});
});

// UX4-1/FE5-1 review fix: the timeout only released the DIALOG - it never
// cancelled the stalled setAgentModel (call() has no AbortSignal to do it
// with). A retry (same or different pick) used to be allowed to race that
// stale request, and close+reopen could even bypass the wait-guard entirely
// (FE5-1) or auto-reseed over an unsent new pick. The redesign makes a second
// write impossible instead of racing it: Change/Install stay disabled - across
// a close+reopen too - until the stale request actually settles (or a hard
// limit forces it closed), while Cancel/Close stay enabled throughout.
describe("AgentInstallDialog stale-save handling after a timeout (FE5-1/FE5-2/UX5-1)", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("a stalled save disables Change/Install, keeps Cancel/Close enabled, and announces the note via aria-live", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w); // picks openai/gpt-5, Install -> stalls

		await vi.advanceTimersByTimeAsync(15000);
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledWith(
			"Still saving your model choice. Wait a moment, then try again."
		);

		expect(w.find('[data-label="Change"]').attributes("disabled")).toBeDefined();
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();
		expect(w.find('[data-label="Cancel"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[data-label="Close"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[role="status"]').text()).toBe("Still saving your model choice…");
	});

	it("clicking the disabled Install while pending never sends a second setAgentModel", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await vi.advanceTimersByTimeAsync(15000);

		await w.find('[data-label="Install"]').trigger("click");
		await w.find('[data-label="Install"]').trigger("click");
		await flushPromises();

		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1);
		expect(w.emitted("confirm")).toBeUndefined();
	});

	it("Change/Install stay disabled across a close+reopen while the stale request is still pending", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		apiAgents.getAgentModel.mockResolvedValue({ choice: null });
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await vi.advanceTimersByTimeAsync(15000);

		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();

		expect(w.find('[data-label="Change"]').attributes("disabled")).toBeDefined();
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();
		expect(w.find('[role="status"]').text()).toBe("Still saving your model choice…");
	});

	it("once the stale request settles while open, it re-syncs from the server and re-enables Change/Install", async () => {
		mockTwoModels();
		let resolveStale;
		apiAgents.setAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveStale = resolve;
			})
		);
		apiAgents.getAgentModel.mockResolvedValue({
			choice: { provider: "openai", model: "gpt-5", label: "GPT-5", cost_note: "" },
		});
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await vi.advanceTimersByTimeAsync(15000);

		resolveStale({});
		await flushPromises();

		expect(apiAgents.getAgentModel).toHaveBeenCalledWith("close-auditor");
		expect(w.find('[data-label="Change"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[role="status"]').text()).toBe("");
	});

	// Correctness: a reopen re-seeds picked/autoPicked to the SAME value
	// (draftChanged goes false) purely because it re-fetches the current
	// tenant-wide row - which still reflects openai/gpt-5, the very pick the
	// stale request is writing. The pending gate must still block Install here,
	// or it would fall into the "!draftChanged -> emit confirm" path and
	// install immediately while the write is unresolved.
	// Note: the Install button is genuinely inert while `disabled` (JSDOM, like
	// real browsers, never dispatches click to a disabled element's listeners -
	// verified separately), so this scenario can only be reached in the UI if
	// that binding is ever refactored away. Calling the exposed confirmInstall
	// directly exercises the internal ordering as the real backstop it is.
	it("blocks Install even when a reopen re-seeds the draft back to matching auto-pick while still pending", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		apiAgents.getAgentModel.mockResolvedValue({
			choice: { provider: "openai", model: "gpt-5", label: "GPT-5", cost_note: "" },
		});
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w); // picks openai/gpt-5, Install -> stalls
		await vi.advanceTimersByTimeAsync(15000);

		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.text()).toContain("GPT-5"); // reseeded: picked === autoPicked now
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();

		await w.vm.confirmInstall(); // bypass the (inert) disabled button on purpose
		await flushPromises();

		expect(apiAgents.setAgentModel).toHaveBeenCalledTimes(1); // no 2nd call
		expect(w.emitted("confirm")).toBeUndefined();
	});

	// CR5-4: the reopen path's OWN getAgentModel round-trip (ahead of load())
	// must also keep Install from sitting enabled next to an emptied box.
	it("reopen after a timeout keeps Install disabled during the getAgentModel round-trip", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		let resolveGetAgentModel;
		apiAgents.getAgentModel.mockReturnValue(
			new Promise((resolve) => {
				resolveGetAgentModel = resolve;
			})
		);
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await vi.advanceTimersByTimeAsync(15000);

		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();

		expect(w.text()).toContain("Loading eligible models");
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();

		resolveGetAgentModel({
			choice: { provider: "openai", model: "gpt-5", label: "GPT-5", cost_note: "" },
		});
		await flushPromises();

		expect(w.text()).toContain("GPT-5");
	});

	it("re-fetches the tenant-wide choice on reopen after a timeout, instead of trusting stale modelInfo", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // stalls forever
		apiAgents.getAgentModel.mockResolvedValue({
			choice: {
				provider: "openai",
				model: "gpt-5",
				label: "GPT-5",
				capability_tier: "Advanced",
				cost_note: "",
			},
		});
		const w = mountDialog({ modelInfo: null });
		await flushPromises();
		await startChangedInstall(w); // picks openai/gpt-5, Install -> stalls
		await vi.advanceTimersByTimeAsync(15000);

		// Close and reopen (props.modelInfo is still null - the parent hasn't
		// re-fetched it).
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();

		expect(apiAgents.getAgentModel).toHaveBeenCalledWith("close-auditor");
		expect(w.text()).toContain("GPT-5");
		expect(w.text()).toContain("Already set for everyone using this agent.");
	});

	// Resilience: frappe-ui's call() has no timeout of its own - a request that
	// never settles at all (network black hole) must not hold Change/Install
	// disabled until a page reload.
	it("a request that never settles is force-released after the hard limit, re-syncing and re-enabling controls", async () => {
		mockTwoModels();
		apiAgents.setAgentModel.mockReturnValue(new Promise(() => {})); // never settles
		apiAgents.getAgentModel.mockResolvedValue({
			choice: { provider: "openai", model: "gpt-5", label: "GPT-5", cost_note: "" },
		});
		const w = mountDialog();
		await flushPromises();
		await startChangedInstall(w);
		await vi.advanceTimersByTimeAsync(15000); // the 15s client-side timeout
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeDefined();

		await vi.advanceTimersByTimeAsync(90000); // the hard limit
		await flushPromises();

		expect(apiAgents.getAgentModel).toHaveBeenCalledWith("close-auditor");
		expect(w.find('[data-label="Change"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[data-label="Install"]').attributes("disabled")).toBeUndefined();
		expect(w.find('[role="status"]').text()).toBe("");
	});
});
