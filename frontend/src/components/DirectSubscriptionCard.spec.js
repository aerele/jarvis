import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	getModelCatalogUi: vi.fn(),
	beginPasteSignin: vi.fn(),
	completePasteSignin: vi.fn(),
	disconnectSubscription: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="stub-badge" :data-theme="theme">{{ label }}</span>`,
	},
}));
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm: async () => true }),
}));

import DirectSubscriptionCard from "./DirectSubscriptionCard.vue";

const status = {
	connected: true,
	provider: "OpenAI",
	model: "gpt-5.6",
	account_email: "d@x.com",
	connected_at: "",
};

beforeEach(() => {
	vi.clearAllMocks();
	api.getModelCatalogUi.mockResolvedValue({
		catalog_available: true,
		subscription_connect_providers: [{ provider: "OpenAI", models: ["gpt-5.6"] }],
	});
	api.beginPasteSignin.mockResolvedValue({
		ok: true,
		data: { nonce: "n1", authorize_url: "https://auth.example/x", expires_in: 600 },
	});
});

describe("DirectSubscriptionCard", () => {
	it("labels the sign-in button Reconnect, never Re-authorize", async () => {
		const w = mount(DirectSubscriptionCard, { props: { status } });
		await flushPromises();
		const labels = w.findAll("button").map((b) => b.text());
		expect(labels).toContain("Reconnect");
		expect(w.text()).not.toContain("Re-authorize");
	});

	it("shows the Sign-in expired badge and the line only for an expired entry", async () => {
		const quiet = mount(DirectSubscriptionCard, { props: { status } });
		await flushPromises();
		expect(quiet.find(".stub-badge").exists()).toBe(false);
		const expired = mount(DirectSubscriptionCard, {
			props: { status, expired: { account_ref: "direct:openai", since: 0, fallback: "" } },
		});
		await flushPromises();
		expect(expired.find(".stub-badge").text()).toBe("Sign-in expired");
		expect(expired.text()).toContain("Chats fail until you reconnect.");
	});

	it("autoStart begins the sign-in without a click", async () => {
		const w = mount(DirectSubscriptionCard, { props: { status, autoStart: true } });
		await flushPromises();
		expect(api.beginPasteSignin).toHaveBeenCalledWith("OpenAI", "gpt-5.6");
		expect(w.text()).toContain("Sign in with your OpenAI account");
	});

	it("does not start a sign-in on its own by default", async () => {
		mount(DirectSubscriptionCard, { props: { status } });
		await flushPromises();
		expect(api.beginPasteSignin).not.toHaveBeenCalled();
	});
});
