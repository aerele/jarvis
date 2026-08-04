import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#456 - promote_installation/demote_installation had no frontend, so a
 * shadow installation (e.g. the custom-app-learning scribe) could never reach
 * live. These pin the three things the issue called out:
 *
 *   1. The real activation_state is shown, not just a passive pill.
 *   2. Both directions are reachable - promote AND demote.
 *   3. A rejection (ceiling / capacity / permissions) surfaces its reason and
 *      leaves the dialog open, instead of failing silently.
 */

const api = vi.hoisted(() => ({
	promoteInstallation: vi.fn(),
	demoteInstallation: vi.fn(),
}));
vi.mock("@/api/agents", () => api);

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "" }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "2 days ago" }),
	getConfig: () => null,
	toast: { success: vi.fn(), error: vi.fn() },
	Badge: { name: "Badge", props: ["label"], template: `<span class="badge">{{ label }}</span>` },
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "theme", "tooltip"],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="dialog"><div class="dialog-title">{{ options && options.title }}</div><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "label", "type", "rows"],
		emits: ["update:modelValue"],
		template: `<textarea :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
}));

import ActivationPanel from "./ActivationPanel.vue";

function baseState(overrides = {}) {
	return {
		activation_state: "shadow",
		reviewer: "reviewer@x.com",
		run_as_user: "reviewer@x.com",
		promoted_by: null,
		promoted_at: null,
		...overrides,
	};
}

function mountPanel(props = {}) {
	return mount(ActivationPanel, {
		props: {
			installationName: "INST-0001",
			agentTitle: "Custom App Learning",
			isScribe: true,
			state: baseState(),
			loading: false,
			fetchError: "",
			canAct: true,
			...props,
		},
	});
}

function findByText(wrapper, selector, text) {
	return wrapper.findAll(selector).find((n) => n.text().includes(text));
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("shows the real activation state honestly", () => {
	it("renders the Shadow pill (not a bare Preview label) while shadow", () => {
		const w = mountPanel({ state: baseState({ activation_state: "shadow" }) });
		expect(w.text()).toContain("Shadow (preview)");
		expect(w.find(".badge").exists()).toBe(false);
	});

	it("renders a Live badge with who signed off once promoted", () => {
		const w = mountPanel({
			state: baseState({
				activation_state: "live",
				promoted_by: "admin@x.com",
				promoted_at: "2026-08-01 10:00:00",
			}),
		});
		expect(w.find(".badge").text()).toBe("Live");
		expect(w.text()).toContain("signed off by admin@x.com");
	});

	it("names the run-as user so promotion's escalation is legible", () => {
		const w = mountPanel({ state: baseState({ run_as_user: "svc-account@x.com" }) });
		expect(w.text()).toContain("svc-account@x.com");
	});

	it("tells a scribe reviewer it cannot run at all while shadow", () => {
		const w = mountPanel({ isScribe: true, state: baseState({ activation_state: "shadow" }) });
		expect(w.text()).toContain("cannot run at all until promoted");
	});
});

describe("promotion is reachable and explains the escalation", () => {
	it("opens a dialog naming who it will run as before promoting", async () => {
		const w = mountPanel({ state: baseState({ run_as_user: "svc-account@x.com" }) });
		await findByText(w, "button", "Promote to live").trigger("click");
		expect(w.find(".dialog").exists()).toBe(true);
		expect(w.find(".dialog-title").text()).toContain("Promote Custom App Learning to live?");
		expect(w.text()).toContain("run unattended");
		expect(w.text()).toContain("svc-account@x.com");
	});

	it("calls promote_installation with the justification and emits the new state", async () => {
		api.promoteInstallation.mockResolvedValue({
			data: { name: "INST-0001", activation_state: "live", promoted_by: "me@x.com" },
		});
		const w = mountPanel();
		await findByText(w, "button", "Promote to live").trigger("click");
		await w.find(".dialog textarea").setValue("reviewed the shadow findings");
		await findByText(w, ".dialog button", "Promote to live").trigger("click");
		await flushPromises();

		expect(api.promoteInstallation).toHaveBeenCalledWith(
			"INST-0001",
			"reviewed the shadow findings"
		);
		expect(w.emitted("promoted")).toBeTruthy();
		expect(w.emitted("promoted")[0][0]).toMatchObject({
			activation_state: "live",
			promoted_by: "me@x.com",
		});
		// the dialog closes on success
		expect(w.find(".dialog").exists()).toBe(false);
	});

	it("disables the action for a viewer who is neither the reviewer nor an admin", () => {
		const w = mountPanel({ canAct: false });
		const btn = findByText(w, "button", "Promote to live");
		expect(btn.attributes("disabled")).toBeDefined();
	});
});

describe("demotion is reachable, not just promotion", () => {
	it("offers a Demote action once live", async () => {
		const w = mountPanel({ state: baseState({ activation_state: "live" }) });
		expect(findByText(w, "button", "Demote to shadow")).toBeTruthy();
		expect(findByText(w, "button", "Promote to live")).toBeUndefined();
	});

	it("calls demote_installation and emits shadow state back", async () => {
		api.demoteInstallation.mockResolvedValue({ data: { activation_state: "shadow" } });
		const w = mountPanel({ state: baseState({ activation_state: "live" }) });
		await findByText(w, "button", "Demote to shadow").trigger("click");
		await findByText(w, ".dialog button", "Demote to shadow").trigger("click");
		await flushPromises();

		expect(api.demoteInstallation).toHaveBeenCalledWith("INST-0001", "");
		expect(w.emitted("demoted")[0][0]).toMatchObject({ activation_state: "shadow" });
	});

	it("clears the stale promoted-by/at stamp locally even if the response omits it", async () => {
		// demote_installation's real response only carries {name, activation_state} -
		// promoted_by/at must not linger from the prior live state.
		api.demoteInstallation.mockResolvedValue({ data: { activation_state: "shadow" } });
		const w = mountPanel({
			state: baseState({
				activation_state: "live",
				promoted_by: "admin@x.com",
				promoted_at: "2026-08-01 10:00:00",
			}),
		});
		await findByText(w, "button", "Demote to shadow").trigger("click");
		await findByText(w, ".dialog button", "Demote to shadow").trigger("click");
		await flushPromises();

		expect(w.emitted("demoted")[0][0]).toMatchObject({ promoted_by: null, promoted_at: null });
	});
});

describe("a rejection surfaces its reason instead of failing silently", () => {
	it("keeps the dialog open and shows the endpoint's message on a ceiling refusal", async () => {
		api.promoteInstallation.mockRejectedValue({
			messages: [
				"Activation budget reached: this customer already has 1 live module(s) and the activation ceiling is 1.",
			],
		});
		const w = mountPanel();
		await findByText(w, "button", "Promote to live").trigger("click");
		await findByText(w, ".dialog button", "Promote to live").trigger("click");
		await flushPromises();

		expect(w.find(".dialog").exists()).toBe(true); // did NOT close
		expect(w.find(".error-message").text()).toContain("Activation budget reached");
		expect(w.emitted("promoted")).toBeFalsy();
	});

	it("does the same for a demote rejection", async () => {
		api.demoteInstallation.mockRejectedValue({
			messages: ["Only the named reviewer or a Jarvis Admin may demote this installation."],
		});
		const w = mountPanel({ state: baseState({ activation_state: "live" }) });
		await findByText(w, "button", "Demote to shadow").trigger("click");
		await findByText(w, ".dialog button", "Demote to shadow").trigger("click");
		await flushPromises();

		expect(w.find(".dialog").exists()).toBe(true);
		expect(w.find(".error-message").text()).toContain("named reviewer or a Jarvis Admin");
	});
});
