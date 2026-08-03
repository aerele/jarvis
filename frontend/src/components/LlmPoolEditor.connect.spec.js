import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The plan-05 D2 CONNECT-transaction contract, editor half:
 *
 *   §10.3 API-key probe gate (P0-09) - onboarding's singleMode row exposes a Test,
 *         and "Start chatting" (canStart) requires a PASSING probe bound to the exact
 *         provider/model/key/base_url for a freshly-typed remote key; local/container
 *         endpoints and stored keys are the truthful exceptions; a failed test keeps
 *         every field.
 *   §10.2 capture rehydrate + wire - load() re-attaches an active server-held OAuth
 *         capture so a reload shows the account connected without a second sign-in;
 *         the editor sends capture_id (never oauth_blob / any token) on save.
 *   §10.4 (editor slice) - footerless save() returns { ok, result } and forwards the
 *         host's idempotency key; it never starts its own poll.
 */

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
	pollPoolAccountSignin: vi.fn(),
	getPendingOauthCaptures: vi.fn(),
	cancelPendingOauthCapture: vi.fn(),
}));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
}));

const answer = vi.hoisted(() => ({ confirm: true }));
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm: async () => answer.confirm }),
	confirm: async () => answer.confirm,
	confirmState: { value: null },
	settleConfirm: () => {},
}));

import LlmPoolEditor from "./LlmPoolEditor.vue";

const clone = (v) => JSON.parse(JSON.stringify(v));
let serverPool;
let captures;

function setPool(models) {
	serverPool = { models: clone(models), preset: "", routing_mode: "failover", proxy_active: true };
}

beforeEach(() => {
	vi.clearAllMocks();
	answer.confirm = true;
	setPool([]);
	captures = [];
	api.getLlmConfig.mockImplementation(async () => clone(serverPool));
	api.getLlmSyncStatus.mockImplementation(async () => ({
		last_sync_status: "",
		pending: false,
		subscription_status: "",
		warnings: [],
		model_statuses: [],
	}));
	api.getPresetCatalog.mockImplementation(async () => []);
	api.getModelCatalogUi.mockImplementation(async () => ({
		api_key_models: {},
		subscription_models: {},
		default_models: {},
	}));
	api.saveLlmPool.mockImplementation(async () => ({
		apply_operation: { operation_id: "op1", state: "pending" },
		idempotency_key: "K",
		resumable: false,
		mode: "operation",
	}));
	api.getPendingOauthCaptures.mockImplementation(async () => ({ ok: true, data: { captures } }));
});

afterEach(() => {
	vi.useRealTimers();
});

async function idle() {
	for (let i = 0; i < 6; i++) {
		await flushPromises();
		await new Promise((r) => setTimeout(r, 1));
	}
	await flushPromises();
}

async function mountOnboarding() {
	const w = mount(LlmPoolEditor, { props: { modes: ["quick"], footerless: true } });
	await idle();
	return w;
}

// Turn the default onboarding subscription row into a filled API-key row.
async function makeApiKeyRow(w, { provider = "openai", model = "gpt-4o", apiKey = "sk-live" } = {}) {
	const r = w.vm.rows[0];
	w.vm.setCredType(r, "api_key");
	r.provider = provider;
	r.model = model;
	r.apiKey = apiKey;
	await flushPromises();
	return w.vm.rows[0];
}

describe("§10.3 API-key probe gate (P0-09)", () => {
	it("a freshly-typed remote key cannot Start until it has a PASSING probe", async () => {
		const w = await mountOnboarding();
		await makeApiKeyRow(w);
		// Fields are all present, but nothing has probed the key yet.
		expect(w.vm.canStart).toBe(false);
		expect(w.vm.startBlockedReason).toMatch(/test/i);

		api.testLlmApiKey.mockResolvedValue({ ok: true, checks: [{ detail: "accepted" }] });
		await w.vm.testSingleModeRow();
		await flushPromises();

		expect(w.vm.smTest.result.ok).toBe(true);
		expect(w.vm.canStart).toBe(true);
		expect(w.vm.startBlockedReason).toBe("");
	});

	it("editing the key AFTER a pass invalidates it (probe is bound to the fields)", async () => {
		const w = await mountOnboarding();
		const r = await makeApiKeyRow(w);
		api.testLlmApiKey.mockResolvedValue({ ok: true, checks: [{ detail: "accepted" }] });
		await w.vm.testSingleModeRow();
		await flushPromises();
		expect(w.vm.canStart).toBe(true);

		r.apiKey = "sk-different";
		await flushPromises();

		expect(w.vm.canStart).toBe(false);
		expect(w.vm.smTest.result).toBe(null); // the green check is gone
		expect(w.vm.startBlockedReason).toMatch(/test/i);
	});

	it("re-typing the EXACT previously-probed key still requires a re-probe (pass is reset, not just shadowed)", async () => {
		const w = await mountOnboarding();
		const r = await makeApiKeyRow(w); // apiKey "sk-live"
		api.testLlmApiKey.mockResolvedValue({ ok: true, checks: [{ detail: "accepted" }] });
		await w.vm.testSingleModeRow();
		await flushPromises();
		expect(w.vm.canStart).toBe(true);

		// Edit AWAY, then back to the exact value the pass was earned on.
		r.apiKey = "sk-different";
		await flushPromises();
		expect(w.vm.canStart).toBe(false);

		r.apiKey = "sk-live";
		await flushPromises();
		// The stored pass was CLEARED on the first edit, so identity-equality alone must
		// not re-enable Start - the customer must probe the reinstated key again. (A
		// mutant that drops the passIdentity reset would show canStart=true here with no
		// visible green probe.)
		expect(w.vm.canStart).toBe(false);
		expect(w.vm.smTest.result).toBe(null);
		expect(w.vm.startBlockedReason).toMatch(/test/i);
	});

	it("a local provider (ollama) needs NO probe to Start", async () => {
		const w = await mountOnboarding();
		await makeApiKeyRow(w, { provider: "ollama", model: "llama3", apiKey: "" });
		expect(w.vm.canStart).toBe(true);
		expect(w.vm.startBlockedReason).toBe("");
	});

	it("a failed test preserves every entered field (the key is never cleared)", async () => {
		const w = await mountOnboarding();
		const r = await makeApiKeyRow(w);
		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			checks: [{ detail: "Insufficient balance." }],
		});
		await w.vm.testSingleModeRow();
		await flushPromises();

		expect(w.vm.smTest.result.ok).toBe(false);
		expect(r.apiKey).toBe("sk-live"); // untouched
		expect(r.provider).toBe("openai");
		expect(r.model).toBe("gpt-4o");
		expect(w.vm.canStart).toBe(false);
	});
});

describe("§10.2 capture rehydrate + wire", () => {
	it("load() re-attaches an active capture so the row shows connected without re-signin", async () => {
		captures = [
			{
				capture_id: "cap1",
				account_ref: "SUB_x",
				label: "me@x.com",
				account_email: "me@x.com",
				upstream: "openai",
			},
		];
		const w = await mountOnboarding();

		const r = w.vm.rows[0];
		expect(r.credentialType).toBe("subscription");
		expect(r.accounts).toHaveLength(1);
		expect(r.accounts[0].account_ref).toBe("SUB_x");
		expect(r.accounts[0].capture_id).toBe("cap1");
		// A connected capture is enough to Start (no probe needed for a subscription).
		expect(w.vm.canStart).toBe(true);
	});

	it("buildSaveModels sends capture_id for a fresh account and neither for a stored one", async () => {
		const w = await mountOnboarding();
		const r = w.vm.rows[0]; // subscription by default
		if (!Array.isArray(r.accounts)) r.accounts = [];
		r.accounts.push({ upstream: "openai", account_ref: "FRESH", label: "a@x", capture_id: "cap9" });
		r.accounts.push({ upstream: "openai", account_ref: "STORED", label: "b@x", capture_id: "" });
		await flushPromises();

		const models = w.vm.buildSaveModels(w.vm.rows);
		const accts = models[0].subscription.accounts;
		const fresh = accts.find((a) => a.account_ref === "FRESH");
		const stored = accts.find((a) => a.account_ref === "STORED");
		expect(fresh.capture_id).toBe("cap9");
		expect(stored.capture_id).toBe(""); // stored: neither a blob nor a live capture
		// The blob NEVER crosses the wire, on any account.
		expect(JSON.stringify(models)).not.toMatch(/oauth_blob|access_token|refresh_token/);
	});

	it("no oauth_blob / token string ever appears in the payload the editor sends", async () => {
		const w = await mountOnboarding();
		const r = w.vm.rows[0];
		if (!Array.isArray(r.accounts)) r.accounts = [];
		r.accounts.push({
			upstream: "openai",
			account_ref: "SUB_a",
			label: "a@x",
			account_email: "a@x",
			capture_id: "capA",
		});
		await flushPromises();

		const res = await w.vm.save("IDEM-1");
		expect(res.ok).toBe(true);
		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
		const [modelsArg, , routingArg, idemArg] = api.saveLlmPool.mock.calls[0];
		expect(JSON.stringify(modelsArg)).not.toMatch(/oauth_blob|access_token|refresh_token/);
		expect(JSON.stringify(modelsArg)).toMatch(/capA/); // the capture id IS what is sent
		expect(routingArg).toBe("failover");
		expect(idemArg).toBe("IDEM-1"); // the host's idempotency key is forwarded
	});

	it("footerless save() returns { ok, result } and never starts its own poll", async () => {
		const w = await mountOnboarding();
		const r = w.vm.rows[0];
		if (!Array.isArray(r.accounts)) r.accounts = [];
		r.accounts.push({ upstream: "openai", account_ref: "SUB_a", label: "a@x", capture_id: "capA" });
		await flushPromises();

		const before = api.getLlmSyncStatus.mock.calls.length;
		const res = await w.vm.save("K2");
		await idle();

		expect(res).toMatchObject({ ok: true });
		expect(res.result).toMatchObject({ idempotency_key: "K", resumable: false, mode: "operation" });
		expect(res.result.apply_operation.operation_id).toBe("op1");
		// The editor is NOT the observer in footerless mode: it does not poll sync status.
		expect(api.getLlmSyncStatus.mock.calls.length).toBe(before);
	});
});
