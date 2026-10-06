import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	getLlmConfig: vi.fn(),
	getLlmSyncStatus: vi.fn(),
	getPresetCatalog: vi.fn(),
	getModelCatalogUi: vi.fn(),
	saveLlmPool: vi.fn(),
	disconnectLlm: vi.fn(),
	testLlmApiKey: vi.fn(),
	disconnectSubscription: vi.fn(),
	getDirectSubscriptionStatus: vi.fn(),
	beginPoolAccountSignin: vi.fn(),
	completePoolAccountSignin: vi.fn(),
	beginClaudeCliLogin: vi.fn(),
	completeClaudeCliLogin: vi.fn(),
	cancelClaudeCliLogin: vi.fn(),
	beginPasteSignin: vi.fn(),
	completePasteSignin: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({
		format: () => "Tue, Oct 6, 2026 5:30 PM",
		fromNow: () => "",
		isValid: () => false,
	}),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<span/>" },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="stub-badge" :data-theme="theme" :data-label="label">{{ label }}</span>`,
	},
}));
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm: async () => true }),
	confirm: async () => true,
	confirmState: { value: null },
	settleConfirm: () => {},
}));

import LlmPoolEditor from "./LlmPoolEditor.vue";
import { MODEL_CATALOG_UI } from "@/lib/__fixtures__/modelCatalogUi.fixtures.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const account = (ref, email) => ({
	upstream: "openai",
	account_ref: ref,
	label: email,
	account_email: email,
});
const subModel = (model, order, accounts) => ({
	model,
	order,
	subscription: { rotation: "sticky", accounts },
});
const keyModel = (provider, model, order) => ({ provider, model, order, has_key: true });
const entry = (ref, extra = {}) => ({
	account_ref: ref,
	upstream: "openai",
	label: "OpenAI",
	email: "",
	state: "expired",
	source: "poll",
	since: 1791230000,
	fallback: "",
	...extra,
});

let pool;
beforeEach(() => {
	vi.clearAllMocks();
	pool = { models: [], preset: "", routing_mode: "failover", proxy_active: true };
	api.getLlmConfig.mockImplementation(async () => clone(pool));
	api.getLlmSyncStatus.mockImplementation(async () => ({
		last_sync_status: "ok (restart via admin)",
		pending: false,
		subscription_status: "",
		warnings: [],
		model_statuses: [],
	}));
	api.getPresetCatalog.mockImplementation(async () => []);
	api.getModelCatalogUi.mockImplementation(async () => clone(MODEL_CATALOG_UI));
});

async function mountEditor(props = {}) {
	const w = mount(LlmPoolEditor, { props });
	for (let i = 0; i < 6; i++) {
		await flushPromises();
		await new Promise((r) => setTimeout(r, 1));
	}
	return w;
}
const badges = (w) =>
	w.findAll(".stub-badge").filter((b) => b.attributes("data-label") === "Sign-in expired");

describe("an expired single-account row", () => {
	it("shows the red badge, the fallback line and a primary Reconnect", async () => {
		pool.models = [
			subModel("gpt-5.6", 0, [account("A1", "a@x.com")]),
			keyModel("Anthropic", "claude-x", 1),
		];
		const w = await mountEditor({ expiredEntries: [entry("A1", { fallback: "claude-x" })] });
		expect(badges(w)).toHaveLength(1);
		expect(badges(w)[0].attributes("data-theme")).toBe("red");
		expect(w.find(".jv-flist-expline").text()).toContain(
			"claude-x answers until you reconnect.",
		);
		const reconnect = w
			.findAll("button")
			.find((b) => b.text() === "Reconnect" && b.classes().includes("jv-btn--primary"));
		expect(reconnect).toBeTruthy();
	});

	it("renders the expiry line as plain text, not a live region announced on mount", async () => {
		pool.models = [subModel("gpt-5.6", 0, [account("A1", "a@x.com")])];
		const w = await mountEditor({ expiredEntries: [entry("A1")] });
		expect(w.find(".jv-flist-expline").attributes("role")).toBeUndefined();
	});

	it("dates the expiry in the browser's local time", async () => {
		pool.models = [
			subModel("gpt-5.6", 0, [account("A1", "a@x.com")]),
			keyModel("Anthropic", "claude-x", 1),
		];
		const w = await mountEditor({
			expiredEntries: [entry("A1", { fallback: "claude-x", since: 1791230000 })],
		});
		const day = new Date(1791230000 * 1000).toLocaleDateString(undefined, {
			year: "numeric",
			month: "short",
			day: "numeric",
		});
		expect(w.find(".jv-flist-expline").text()).toMatch(new RegExp(`^Expired ${day}\\. `));
	});

	it("says chats fail when nothing else can answer", async () => {
		pool.models = [subModel("gpt-5.6", 0, [account("A1", "a@x.com")])];
		const w = await mountEditor({ expiredEntries: [entry("A1")] });
		expect(w.find(".jv-flist-expline").text()).toBe("Chats fail until you reconnect.");
	});

	it("shows nothing extra for a healthy pool", async () => {
		pool.models = [subModel("gpt-5.6", 0, [account("A1", "a@x.com")])];
		const w = await mountEditor();
		expect(badges(w)).toHaveLength(0);
		expect(w.find(".jv-flist-expline").exists()).toBe(false);
	});
});

describe("two accounts on one row, one expired (Review Focus 5)", () => {
	it("badges only the dead account and says another account answers", async () => {
		pool.models = [
			subModel("gpt-5.6", 0, [account("A1", "a@x.com"), account("A2", "b@x.com")]),
		];
		const w = await mountEditor({ expiredEntries: [entry("A1", { fallback: "Anthropic" })] });
		expect(badges(w)).toHaveLength(1);
		expect(w.find(".jv-flist-subrow-note").text()).toBe(
			"Another account answers until you reconnect.",
		);
		expect(w.find(".jv-flist-expline").exists()).toBe(false);
		expect(w.text()).not.toContain("Chats fail until you reconnect.");
	});
});

describe("the reconnect deep link (I8)", () => {
	it("opens the sign-in for the row holding that account and names the account to use", async () => {
		pool.models = [
			subModel("gpt-5.6", 0, [account("A1", "a@x.com"), account("A2", "b@x.com")]),
		];
		const w = await mountEditor({ expiredEntries: [entry("A2")], reconnectRef: "A2" });
		expect(w.emitted("reconnect-handled")).toBeTruthy();
		expect(w.text()).toContain("Use the same account: b@x.com");
	});

	it("does nothing for a ref this pool does not hold", async () => {
		pool.models = [subModel("gpt-5.6", 0, [account("A1", "a@x.com")])];
		const w = await mountEditor({ reconnectRef: "ZZ" });
		expect(w.emitted("reconnect-handled")).toBeFalsy();
		expect(w.text()).not.toContain("Use the same account");
	});
});

describe("direct mode (lone OpenAI subscription, key direct:openai)", () => {
	const directStatus = {
		is_direct_subscription: true,
		connected: true,
		provider: "OpenAI",
		model: "gpt-5.6",
		account_email: "d@x.com",
		auth_mode: "oauth",
	};

	it("badges the direct row from the direct:openai entry", async () => {
		const w = await mountEditor({
			directStatus,
			expiredEntries: [entry("direct:openai", { email: "d@x.com" })],
		});
		expect(badges(w).length).toBeGreaterThanOrEqual(1);
		expect(w.text()).toContain("Chats fail until you reconnect.");
	});

	it("the deep link opens the direct panel and starts its sign-in", async () => {
		api.beginPasteSignin.mockResolvedValue({
			ok: true,
			data: { nonce: "n1", authorize_url: "https://auth.example/x", expires_in: 600 },
		});
		const w = await mountEditor({
			directStatus,
			expiredEntries: [entry("direct:openai")],
			reconnectRef: "direct:openai",
		});
		expect(w.emitted("reconnect-handled")).toBeTruthy();
		expect(api.beginPasteSignin).toHaveBeenCalledTimes(1);
	});

	it("auto-starts only from the intent: closing and reopening by hand does not re-run it", async () => {
		api.beginPasteSignin.mockResolvedValue({
			ok: true,
			data: { nonce: "n1", authorize_url: "https://auth.example/x", expires_in: 600 },
		});
		const w = await mountEditor({
			directStatus,
			expiredEntries: [entry("direct:openai")],
			reconnectRef: "direct:openai",
		});
		expect(api.beginPasteSignin).toHaveBeenCalledTimes(1);
		const toggle = () =>
			w.findAll("button").find((b) => ["Close", "Reconnect"].includes(b.text()));
		await toggle().trigger("click");
		await flushPromises();
		await toggle().trigger("click");
		await flushPromises();
		expect(api.beginPasteSignin).toHaveBeenCalledTimes(1);
	});
});
