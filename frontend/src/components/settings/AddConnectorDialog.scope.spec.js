import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

// Who can use a new connector: Only me unless an admin pressed the Shared
// section's Add button. A plain user never sees the control at all.
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

async function openDialog(scope) {
	const props = { modelValue: false, preset: "Custom URL", catalog: [] };
	if (scope) props.scope = scope;
	const wrapper = mount(AddConnectorDialog, { props });
	await wrapper.setProps({ modelValue: true });
	return wrapper;
}

describe("AddConnectorDialog: initial scope", () => {
	beforeEach(() => {
		window.is_system_manager = true;
		window.is_jarvis_admin = false;
	});
	afterEach(() => {
		delete window.is_system_manager;
		delete window.is_jarvis_admin;
	});

	it("an admin gets Only me when the pane passes no scope", async () => {
		const wrapper = await openDialog();
		expect(wrapper.find(".scope-control").attributes("data-value")).toBe("Personal");
	});

	it("an admin gets Only me from the Mine section", async () => {
		const wrapper = await openDialog("Personal");
		expect(wrapper.find(".scope-control").attributes("data-value")).toBe("Personal");
	});

	it("an admin gets Everyone from the Shared section", async () => {
		const wrapper = await openDialog("Shared");
		expect(wrapper.find(".scope-control").attributes("data-value")).toBe("Shared");
	});

	it("a plain user never sees the control, even if Shared is passed", async () => {
		window.is_system_manager = false;
		const wrapper = await openDialog("Shared");
		expect(wrapper.find(".scope-control").exists()).toBe(false);
		expect(wrapper.text()).not.toContain("Who can use it");
	});

	it("the caption follows the selection", async () => {
		const shared = await openDialog("Shared");
		expect(shared.text()).toContain("Everyone in your workspace can ask Jarvis to use this.");
		const mine = await openDialog("Personal");
		expect(mine.text()).toContain("Only you can use this.");
		expect(mine.text()).not.toContain("Everyone in your workspace");
	});

	it("a plain user sees no caption", async () => {
		window.is_system_manager = false;
		const wrapper = await openDialog("Shared");
		expect(wrapper.text()).not.toContain("Only you can use this.");
		expect(wrapper.text()).not.toContain("Everyone in your workspace");
	});
});
