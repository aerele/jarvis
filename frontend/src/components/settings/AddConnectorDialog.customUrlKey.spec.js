import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// A Custom URL whose server advertises a sign-in must still let the person paste
// a key: "Use a key instead" swaps the card, and the way back names the server.
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
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
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

async function checkedCustomUrl() {
	const wrapper = mount(AddConnectorDialog, {
		props: { modelValue: false, preset: "Custom URL", catalog: [] },
	});
	await wrapper.setProps({ modelValue: true });
	await wrapper
		.find('input[aria-label="Base URL"]')
		.setValue("https://mcp.zoom.us/mcp/zoom/streamable");
	await byLabel(wrapper, "Check").trigger("click");
	await flushPromises();
	return wrapper;
}

describe("AddConnectorDialog: Custom URL key vs sign-in", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.probeConnectorAuth.mockResolvedValue({
			ok: true,
			needs_signin: true,
			signin_host: "zoom.us",
			registration: "dynamic",
		});
	});

	it("opens on the sign-in card when the server advertises one", async () => {
		const wrapper = await checkedCustomUrl();
		expect(byLabel(wrapper, "Sign in with zoom.us").exists()).toBe(true);
		expect(wrapper.find('input[aria-label="Key"]').exists()).toBe(false);
	});

	it("Use a key instead swaps to the key card, and back again", async () => {
		const wrapper = await checkedCustomUrl();

		await byLabel(wrapper, "Use a key instead").trigger("click");
		await flushPromises();
		expect(wrapper.find('input[aria-label="Key"]').exists()).toBe(true);
		expect(byLabel(wrapper, "Sign in with zoom.us").exists()).toBe(false);
		expect(byLabel(wrapper, "Sign in with zoom.us instead").exists()).toBe(true);

		await byLabel(wrapper, "Sign in with zoom.us instead").trigger("click");
		await flushPromises();
		expect(byLabel(wrapper, "Sign in with zoom.us").exists()).toBe(true);
		expect(wrapper.find('input[aria-label="Key"]').exists()).toBe(false);
	});

	it("a server with no sign-in goes straight to the key card", async () => {
		api.probeConnectorAuth.mockResolvedValue({ ok: true, needs_signin: false });
		const wrapper = await checkedCustomUrl();
		expect(wrapper.find('input[aria-label="Key"]').exists()).toBe(true);
		expect(byLabel(wrapper, "Use a key instead").exists()).toBe(false);
	});
});

describe("AddConnectorDialog: presets that refuse a key", () => {
	const entry = (name, key, accepts_key) => ({
		name,
		key,
		auth: "static",
		category: "files",
		description: name,
		accepts_key,
	});
	const catalog = [
		entry("Google Drive", "google_drive", false),
		entry("GitHub", "github", true),
	];

	async function opened(preset) {
		const wrapper = mount(AddConnectorDialog, {
			props: { modelValue: false, preset, catalog },
		});
		await wrapper.setProps({ modelValue: true });
		await flushPromises();
		return wrapper;
	}

	it("hides Use a key instead for a sign-in-only preset", async () => {
		const wrapper = await opened("Google Drive");
		expect(byLabel(wrapper, "Use a key instead").exists()).toBe(false);
	});

	it("keeps Use a key instead for a sign-in preset that takes keys", async () => {
		const wrapper = await opened("GitHub");
		expect(byLabel(wrapper, "Use a key instead").exists()).toBe(true);
	});
});
