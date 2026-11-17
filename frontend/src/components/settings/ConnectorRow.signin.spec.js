import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

// The real oauthSignin runs here (only its api calls are mocked), so the
// assertions about polling are about the real poll loop.
const api = vi.hoisted(() => ({
	connectOauth: vi.fn(),
	oauthSigninStatus: vi.fn(),
	disconnectOauth: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("@/components/settings/ConnectorLogo.vue", () => ({
	default: { name: "ConnectorLogo", template: `<span />` },
}));
vi.mock("frappe-ui", () => ({
	Badge: { name: "Badge", props: ["label"], template: `<span>{{ label }}</span>` },
	Switch: { name: "Switch", template: `<span />` },
	Tooltip: { name: "Tooltip", template: `<span><slot /></span>` },
	Button: {
		name: "Button",
		props: ["label", "loading"],
		emits: ["click"],
		template: `<button :data-label="label" :data-loading="String(!!loading)" @click="$emit('click')">{{ label }}</button>`,
	},
	confirmDialog: vi.fn(),
	toast: { error: vi.fn(), success: vi.fn() },
}));

import { toast } from "frappe-ui";
import ConnectorRow from "./ConnectorRow.vue";

const STARTED_AT = "2026-01-01T00:00:00.000Z";
const row = {
	name: "row-1",
	label: "Sentry",
	preset: "Sentry",
	enabled: 1,
	auth_method: "OAuth",
	oauth_configured: 1,
	oauth_connected: 0,
};

function fakeTab() {
	const tab = {
		closed: false,
		opener: {},
		document: { write: vi.fn(), close: vi.fn() },
		location: {},
	};
	tab.close = vi.fn(() => {
		tab.closed = true;
	});
	return tab;
}

describe("ConnectorRow sign-in", () => {
	let tabs;
	let openSpy;

	beforeEach(() => {
		vi.useFakeTimers();
		vi.clearAllMocks();
		tabs = [];
		openSpy = vi.spyOn(window, "open").mockImplementation(() => {
			const t = fakeTab();
			tabs.push(t);
			return t;
		});
		api.connectOauth.mockResolvedValue({
			ok: true,
			url: "https://vendor.example/authorize",
			started_at: STARTED_AT,
		});
		api.oauthSigninStatus.mockResolvedValue({ ok: true, connected: false });
	});
	afterEach(() => {
		openSpy.mockRestore();
		vi.useRealTimers();
	});

	const signInButton = (w) => w.find('button[data-label="Sign in"]');

	it("stops polling and shows no toast when the row unmounts mid-poll", async () => {
		const wrapper = mount(ConnectorRow, { props: { row } });
		await signInButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(4000);
		const calls = api.oauthSigninStatus.mock.calls.length;
		expect(calls).toBeGreaterThan(0);

		wrapper.unmount();
		expect(tabs[0].close).toHaveBeenCalled(); // cancel() closes the tab it opened

		await vi.advanceTimersByTimeAsync(30000);
		expect(api.oauthSigninStatus.mock.calls.length).toBe(calls);
		expect(toast.error).not.toHaveBeenCalled();
	});

	it("leaves loading once the tab looks closed, and a press restarts with one poll only", async () => {
		const wrapper = mount(ConnectorRow, { props: { row } });
		await signInButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(0);
		expect(signInButton(wrapper).attributes("data-loading")).toBe("true");

		tabs[0].closed = true; // COOP-severed or really closed
		await vi.advanceTimersByTimeAsync(2000);
		expect(signInButton(wrapper).attributes("data-loading")).toBe("false");

		await signInButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(0);
		expect(api.connectOauth).toHaveBeenCalledTimes(2);
		expect(tabs).toHaveLength(2);
		expect(signInButton(wrapper).attributes("data-loading")).toBe("true");

		// One poll loop is left: each 2 s interval makes exactly one status call.
		const before = api.oauthSigninStatus.mock.calls.length;
		await vi.advanceTimersByTimeAsync(10000);
		expect(api.oauthSigninStatus.mock.calls.length - before).toBe(5);
		expect(toast.error).not.toHaveBeenCalled();

		wrapper.unmount();
	});
});
