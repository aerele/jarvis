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
	getLlmApplyOperation: vi.fn(),
}));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
	// Banner.vue (the subscription Test result banner, below) needs this.
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<span/>" },
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
	serverPool = {
		models: clone(models),
		preset: "",
		routing_mode: "failover",
		proxy_active: true,
	};
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
async function makeApiKeyRow(
	w,
	{ provider = "openai", model = "gpt-4o", apiKey = "sk-live" } = {}
) {
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

	it("a local provider pointed at a REMOTELY-reachable base URL still needs a probe (F7)", async () => {
		const w = await mountOnboarding();
		const r = await makeApiKeyRow(w, { provider: "ollama", model: "llama3", apiKey: "sk-x" });
		r.baseUrl = "https://api.openai.com/v1"; // a public endpoint behind a local provider id
		await flushPromises();
		// The local-provider carve-out must NOT short-circuit on the id alone: a
		// remotely-reachable endpoint has to prove a passing probe like any remote key.
		expect(w.vm.canStart).toBe(false);
		expect(w.vm.startBlockedReason).toMatch(/test/i);
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
		r.accounts.push({
			upstream: "openai",
			account_ref: "FRESH",
			label: "a@x",
			capture_id: "cap9",
		});
		r.accounts.push({
			upstream: "openai",
			account_ref: "STORED",
			label: "b@x",
			capture_id: "",
		});
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
		r.accounts.push({
			upstream: "openai",
			account_ref: "SUB_a",
			label: "a@x",
			capture_id: "capA",
		});
		await flushPromises();

		const before = api.getLlmSyncStatus.mock.calls.length;
		const res = await w.vm.save("K2");
		await idle();

		expect(res).toMatchObject({ ok: true });
		expect(res.result).toMatchObject({
			idempotency_key: "K",
			resumable: false,
			mode: "operation",
		});
		expect(res.result.apply_operation.operation_id).toBe("op1");
		// The editor is NOT the observer in footerless mode: it does not poll sync status.
		expect(api.getLlmSyncStatus.mock.calls.length).toBe(before);
	});
});

// Connect a fresh subscription account onto the onboarding row the same way §10.2's
// capture-rehydrate tests do, so testSubscriptionRow has something to run against.
async function connectSubscriptionRow(w, { upstream = "openai" } = {}) {
	const r = w.vm.rows[0];
	r.credentialType = "subscription";
	r.upstream = upstream;
	if (!Array.isArray(r.accounts)) r.accounts = [];
	r.accounts.push({ upstream, account_ref: "SUB_a", label: "a@x", capture_id: "capA" });
	await flushPromises();
	return r;
}

describe("subscription Test (same probe the apply path uses)", () => {
	it("is blocked before an account is connected, and never calls saveLlmPool", async () => {
		const w = await mountOnboarding();
		expect(w.vm.subTestBlockedReason(w.vm.rows[0])).toMatch(/connect/i);
		await w.vm.testSubscriptionRow(w.vm.rows[0]);
		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});

	it("a genuine READY verdict reports success without navigating anywhere itself", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			code: "LLM_READY",
			chat_readiness: true,
		});

		await w.vm.testSubscriptionRow(r);

		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
		expect(w.vm.subTest.result.kind).toBe("ok");
		expect(w.vm.subTest.result.message).toMatch(/openai/i);
		expect(w.vm.subTest.result.message).toMatch(/start chatting/i);
	});

	it("a rejected verdict shows admin's chat_readiness_reason VERBATIM, not a reworded copy", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		const adminReason = "Your OpenAI subscription was rejected. Reconnect the account.";
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "failed",
			code: "LLM_APPLY_REJECTED",
			chat_readiness_reason: adminReason,
		});

		await w.vm.testSubscriptionRow(r);

		expect(w.vm.subTest.result.kind).toBe("fail");
		expect(w.vm.subTest.result.message).toBe(adminReason);
	});

	it("mints its own idempotency key, distinct from any host save() key", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({ operation_id: "op1", state: "ready" });

		await w.vm.testSubscriptionRow(r);
		await w.vm.save("host-idem-key");

		const testCallKey = api.saveLlmPool.mock.calls[0][3];
		const hostCallKey = api.saveLlmPool.mock.calls[1][3];
		expect(testCallKey).not.toBe("");
		expect(testCallKey).not.toBe(hostCallKey);
		expect(hostCallKey).toBe("host-idem-key");
	});

	it("a second click while one is already in flight is a no-op (only one saveLlmPool call)", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		let resolveOp;
		api.getLlmApplyOperation.mockImplementation(
			() => new Promise((resolve) => (resolveOp = resolve))
		);

		const first = w.vm.testSubscriptionRow(r);
		await flushPromises();
		expect(w.vm.subTest.testing).toBe(true);
		const second = w.vm.testSubscriptionRow(r); // must be swallowed, not queued

		resolveOp({ operation_id: "op1", state: "ready", chat_readiness: true });
		await Promise.all([first, second]);

		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
	});

	it("hostBusy (Start-chatting in flight) blocks Test from firing", async () => {
		const w = mount(LlmPoolEditor, {
			props: { modes: ["quick"], footerless: true, hostBusy: true },
		});
		await idle();
		const r = await connectSubscriptionRow(w);

		await w.vm.testSubscriptionRow(r);

		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});

	it("cools down after a result lands, then allows a fresh Test", async () => {
		vi.useFakeTimers({ shouldAdvanceTime: true });
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({ operation_id: "op1", state: "ready" });

		await w.vm.testSubscriptionRow(r);
		expect(w.vm.subTest.cooling).toBe(true);

		await w.vm.testSubscriptionRow(r); // still cooling: swallowed
		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(15000);
		expect(w.vm.subTest.cooling).toBe(false);

		await w.vm.testSubscriptionRow(r);
		expect(api.saveLlmPool).toHaveBeenCalledTimes(2);
	});

	it("emits subscription-testing(true) while running and (false) once settled, for the host's cross-guard", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		let resolveOp;
		api.getLlmApplyOperation.mockImplementation(
			() => new Promise((resolve) => (resolveOp = resolve))
		);

		const p = w.vm.testSubscriptionRow(r);
		await flushPromises();
		expect(w.emitted("subscription-testing")).toEqual([[true]]);

		resolveOp({ operation_id: "op1", state: "ready", chat_readiness: true });
		await p;

		expect(w.emitted("subscription-testing")).toEqual([[true], [false]]);
	});

	it("a bounded timeout with no terminal state reports a neutral pending message, never a false pass or fail", async () => {
		vi.useFakeTimers({ shouldAdvanceTime: true });
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({ operation_id: "op1", state: "pending" });

		const p = w.vm.testSubscriptionRow(r);
		await vi.advanceTimersByTimeAsync(95000); // past SUB_TEST_TIMEOUT_MS
		await p;

		expect(w.vm.subTest.result.kind).toBe("pending");
	});
});

// jarvis_admin_v2#297: admin now accepts force_probe on update_llm_pool so a
// repeat Test can ask for a real re-check instead of admin's byte-identical
// no-op path answering from the last verdict on record. Every ordinary save
// (the settings-pane apply, and "Start chatting" via footerless save()) must
// keep calling saveLlmPool with FEWER than five arguments, so force_probe
// silently defaults to false there and the request they send is unchanged.
describe("subscription Test force_probe (jarvis_admin_v2#297): no repeat can carry a stale verdict", () => {
	it("every Test press asks admin for a forced probe, as the 5th saveLlmPool argument", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			chat_readiness: true,
			force_probed: true,
		});

		await w.vm.testSubscriptionRow(r);

		expect(api.saveLlmPool.mock.calls[0][4]).toBe(true);
	});

	it("footerless save() (Start chatting's path) never asks for a forced probe", async () => {
		const w = await mountOnboarding();
		await connectSubscriptionRow(w);

		await w.vm.save("host-idem-key");

		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
		// Fewer than 5 args: force_probe is not even mentioned on the ordinary path.
		expect(api.saveLlmPool.mock.calls[0].length).toBeLessThan(5);
	});

	it("two Test presses in a row each mint their OWN fresh idempotency key, both requesting a forced probe", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			chat_readiness: true,
			force_probed: true,
		});

		await w.vm.testSubscriptionRow(r);
		w.vm.subTest.cooling = false; // bypass the post-result cooldown, tested elsewhere
		await w.vm.testSubscriptionRow(r);

		const firstKey = api.saveLlmPool.mock.calls[0][3];
		const secondKey = api.saveLlmPool.mock.calls[1][3];
		expect(firstKey).not.toBe("");
		expect(secondKey).not.toBe("");
		expect(secondKey).not.toBe(firstKey);
		expect(api.saveLlmPool.mock.calls[0][4]).toBe(true);
		expect(api.saveLlmPool.mock.calls[1][4]).toBe(true);
	});

	it("force_probed: true reports the fresh-check copy, never the stale one", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			chat_readiness: true,
			force_probed: true,
		});

		await w.vm.testSubscriptionRow(r);

		expect(w.vm.subTest.result.kind).toBe("ok");
		expect(w.vm.subTest.result.message).toMatch(/just now/i);
	});

	it("force_probed: false on a PASS says this is the last check, not a fresh one, without claiming failure", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			chat_readiness: true,
			force_probed: false, // host below fleet-agent contract 1.23 ignored the ask
		});

		await w.vm.testSubscriptionRow(r);

		expect(w.vm.subTest.result.kind).toBe("ok");
		expect(w.vm.subTest.result.message).not.toMatch(/just now/i);
		expect(w.vm.subTest.result.message).toMatch(/last check/i);
		expect(w.vm.subTest.result.message).toMatch(/start chatting/i);
	});

	it("force_probed: false on a FAILURE still quotes admin's reason verbatim, with a stale notice appended after it", async () => {
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		const adminReason = "Your OpenAI account has reached its usage limit.";
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "failed",
			code: "LLM_APPLY_REJECTED",
			chat_readiness_reason: adminReason,
			force_probed: false,
		});

		await w.vm.testSubscriptionRow(r);

		expect(w.vm.subTest.result.kind).toBe("fail");
		expect(w.vm.subTest.result.message.startsWith(adminReason)).toBe(true);
		expect(w.vm.subTest.result.message).not.toBe(adminReason);
	});

	it("a mocked status with no force_probed field at all (older fixture) is treated as fresh, not stale", async () => {
		// Guards the existing verbatim-reason and just-now tests above this describe
		// block, none of which set force_probed: undefined must never satisfy the
		// strict === false stale check.
		const w = await mountOnboarding();
		const r = await connectSubscriptionRow(w);
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "ready",
			chat_readiness: true,
		});

		await w.vm.testSubscriptionRow(r);

		expect(w.vm.subTest.result.message).toMatch(/just now/i);
	});
});

// Connect-error copy: jarvis/oauth/api.py's {ok:false, error:{code, message}} envelope
// carries a raw developer `message` ("nonce not recognized" etc.) alongside a `code`.
// finishConnect (and startConnect / _pollDeviceConnect, same envelope) must show the
// customer the friendly CONNECT_ERROR_COPY text keyed on `code`, not that raw message
// verbatim - and must still show SOMETHING (the raw message) for a code it doesn't know.
describe("connect-flow error copy (finishConnect)", () => {
	async function beginSignin(w, { model = "gpt-4o", upstream = "openai" } = {}) {
		const r = w.vm.rows[0];
		r.credentialType = "subscription";
		r.upstream = upstream;
		r.model = model;
		api.beginPoolAccountSignin.mockResolvedValue({
			ok: true,
			data: { nonce: "nonce1", authorize_url: "https://provider.example/authorize" },
		});
		// openTab: false skips the popup window.open() this test has no need to drive.
		await w.vm.startConnect(r, null, { openTab: false });
		await flushPromises();
		expect(r._connect.nonce).toBe("nonce1"); // sanity: signin actually began
		r._connect.pastedUrl = "https://app.example/callback?code=abc&state=xyz";
		return r;
	}

	it("maps a known code (unknown_nonce) to friendly copy, not the raw backend message", async () => {
		const w = await mountOnboarding();
		const r = await beginSignin(w);
		api.completePoolAccountSignin.mockResolvedValue({
			ok: false,
			error: { code: "unknown_nonce", message: "nonce not recognized" },
		});

		await w.vm.finishConnect(r);

		expect(r._connect.error).not.toBe("nonce not recognized");
		expect(r._connect.error).toMatch(/sign-in session was lost/i);
		expect(r._connect.error).toMatch(/open sign-in/i);
	});

	it("falls back to the backend's own message for an unmapped/unknown code", async () => {
		const w = await mountOnboarding();
		const r = await beginSignin(w);
		api.completePoolAccountSignin.mockResolvedValue({
			ok: false,
			error: { code: "some_future_code", message: "some raw backend text" },
		});

		await w.vm.finishConnect(r);

		expect(r._connect.error).toBe("some raw backend text");
	});

	it("falls back to the generic Connect failure copy when the error has no code or message at all", async () => {
		const w = await mountOnboarding();
		const r = await beginSignin(w);
		api.completePoolAccountSignin.mockResolvedValue({ ok: false });

		await w.vm.finishConnect(r);

		expect(r._connect.error).toMatch(/couldn't connect the account/i);
	});
});
