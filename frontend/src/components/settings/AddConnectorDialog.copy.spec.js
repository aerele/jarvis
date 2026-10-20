import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// Register card copy: step 2 names the right place to create the app.
vi.mock("@/api", () => ({
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
		props: ["label", "variant", "size", "loading", "disabled", "theme"],
		template: `<button>{{ label }}</button>`,
	},
	FormControl: { name: "FormControl", props: ["label", "modelValue"], template: `<input />` },
	Badge: { name: "Badge", props: ["label"], template: `<span>{{ label }}</span>` },
	Switch: { name: "Switch", props: ["modelValue"], template: `<span />` },
	TabButtons: {
		name: "TabButtons",
		props: ["buttons", "modelValue"],
		template: `<span class="scope-control" :data-value="modelValue" />`,
	},
	FeatherIcon: { name: "FeatherIcon", template: `<span />` },
	toast: { error: vi.fn(), success: vi.fn() },
}));

import AddConnectorDialog from "./AddConnectorDialog.vue";

const entry = (name, help_url) => ({
	name,
	key: name.toLowerCase().replace(/ /g, "_"),
	auth: "static",
	category: "files",
	description: name,
	accepts_key: false,
	help_url,
});
const catalog = [
	entry("Google Drive", "https://console.cloud.google.com/apis/credentials"),
	entry("Slack", "https://api.slack.com/apps"),
];

async function opened(preset) {
	const wrapper = mount(AddConnectorDialog, { props: { modelValue: false, preset, catalog } });
	await wrapper.setProps({ modelValue: true });
	await flushPromises();
	return wrapper;
}

describe("AddConnectorDialog: register card step 2", () => {
	beforeEach(() => {
		window.is_system_manager = true;
	});
	afterEach(() => {
		delete window.is_system_manager;
	});

	it("tells a Google app to create the OAuth client in its Google Cloud project", async () => {
		const wrapper = await opened("Google Drive");
		expect(wrapper.text()).toContain("Create the OAuth client in your Google Cloud project");
		expect(wrapper.text()).not.toContain("organisation");
	});

	it("sends any other app to its developer settings", async () => {
		const wrapper = await opened("Slack");
		expect(wrapper.text()).toContain("Create the app in Slack's developer settings");
		expect(wrapper.text()).not.toContain("organisation");
	});
});
