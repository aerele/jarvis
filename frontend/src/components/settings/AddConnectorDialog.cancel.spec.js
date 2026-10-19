import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// Save is the commit point: closing the dialog without Save deletes the row
// this dialog created, even after Connect passed (a kept row stays disabled
// and blocks adding the same app again).
const api = vi.hoisted(() => ({
	addConnector: vi.fn(),
	deleteConnector: vi.fn(),
	disconnectOauth: vi.fn(),
	oauthSigninStatus: vi.fn(),
	probeConnectorAuth: vi.fn(),
	setConnectorAllowedActions: vi.fn(),
	setOauthClientCredentials: vi.fn(),
	testConnector: vi.fn(),
	updateConnector: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("@/components/settings/oauthSignin", () => ({ signIn: vi.fn() }));
vi.mock("@/components/settings/ConnectorLogo.vue", () => ({
	default: { name: "ConnectorLogo", template: `<span />` },
}));

vi.mock("frappe-ui", () => ({
	// Closing fires after-leave, as the real Dialog does once its leave
	// transition ends; that is where the dialog cleans up.
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["after-leave"],
		watch: {
			modelValue(open) {
				if (!open) this.$emit("after-leave");
			},
		},
		template: `<div v-if="modelValue"><slot name="body-content" /><slot name="actions" /></div>`,
	},
	Button: {
		name: "Button",
		props: [
			"label",
			"variant",
			"size",
			"loading",
			"disabled",
			"theme",
			"iconLeft",
			"iconRight",
		],
		emits: ["click"],
		template: `<button :data-label="label" :disabled="disabled" @click="$emit('click')">{{ label }}<slot /></button>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["label", "modelValue", "type", "placeholder"],
		emits: ["update:modelValue"],
		template: `<input :aria-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Badge: { name: "Badge", props: ["label"], template: `<span>{{ label }}</span>` },
	Switch: { name: "Switch", props: ["modelValue"], template: `<span />` },
	TabButtons: { name: "TabButtons", props: ["buttons", "modelValue"], template: `<span />` },
	FeatherIcon: { name: "FeatherIcon", template: `<span />` },
	toast: { error: vi.fn(), success: vi.fn() },
}));

import AddConnectorDialog from "./AddConnectorDialog.vue";

const byLabel = (wrapper, label) => wrapper.find(`button[data-label="${label}"]`);
const catalog = [
	{ name: "Docs Search", key: "docs_search", auth: "open", category: "docs", description: "" },
];

async function connectedOpenPreset() {
	const wrapper = mount(AddConnectorDialog, {
		props: {
			modelValue: false,
			preset: "Docs Search",
			catalog,
			"onUpdate:modelValue": (v) => wrapper.setProps({ modelValue: v }),
		},
	});
	await wrapper.setProps({ modelValue: true });
	await byLabel(wrapper, "Connect").trigger("click");
	await flushPromises();
	return wrapper;
}

describe("AddConnectorDialog: Cancel after a passed Connect", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.addConnector.mockResolvedValue({ name: "row-1" });
		api.deleteConnector.mockResolvedValue({});
		api.testConnector.mockResolvedValue({
			ok: true,
			tools: [{ action: "search_docs", read_only: true }],
		});
		api.setConnectorAllowedActions.mockResolvedValue({});
		api.updateConnector.mockResolvedValue({ name: "row-1", enabled: true });
	});

	it("deletes the row it created", async () => {
		const wrapper = await connectedOpenPreset();
		expect(byLabel(wrapper, "Continue").attributes("disabled")).toBeUndefined();

		await byLabel(wrapper, "Cancel").trigger("click");
		await flushPromises();

		expect(api.deleteConnector).toHaveBeenCalledWith("row-1");
		expect(wrapper.emitted("kept")).toBeUndefined();
	});

	it("keeps the row once Save has run", async () => {
		const wrapper = await connectedOpenPreset();
		await byLabel(wrapper, "Continue").trigger("click");
		await byLabel(wrapper, "Save").trigger("click");
		await flushPromises();

		expect(api.updateConnector).toHaveBeenCalledWith("row-1", { enabled: 1 });
		expect(wrapper.emitted("saved")).toBeTruthy();
		expect(api.deleteConnector).not.toHaveBeenCalled();
	});
});
