import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * LlmPoolEditor: the three behaviours that decide whether "Connect means
 * connected" is actually true.
 *
 *   1. Disconnecting an account PERSISTS. It used to only filter the local array,
 *      so the chip vanished and the account stayed live on the agent until the
 *      next load() put it back on screen.
 *   2. An apply started from one row must not touch the row the customer is
 *      half-way through adding: not save it unfinished, and not let the reseed
 *      that follows delete it.
 *   3. Observation of a slow apply continues after the 90s blocking wait gives
 *      up, so "it will finish on its own" comes true on the screen the customer
 *      is looking at.
 *
 * The fake server below is deliberately real about the round trip: saveLlmPool
 * stores what it was sent and getLlmConfig hands it back, because every one of
 * these defects only shows itself in what load() reseeds afterwards.
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
}));
vi.mock("@/api", () => api);

// frappe-ui is only reachable through @/utils/datetime here (DirectSubscriptionCard),
// and its ESM entry does not resolve under vitest.
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

// The pool the fake server currently holds. saveLlmPool overwrites it, exactly as
// save_llm_pool does, so the load() inside runApply reseeds from what was really sent.
let serverPool;
let syncStatus;

function account(ref, email) {
	return { upstream: "openai", account_ref: ref, label: email, account_email: email };
}
function subModel(model, order, accounts) {
	return { model, order, subscription: { rotation: "sticky", accounts } };
}
function keyModel(provider, model, order) {
	return { provider, model, order, has_key: true };
}

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
	syncStatus = {
		last_sync_status: "ok (restart via admin)",
		pending: false,
		subscription_status: "",
		warnings: [],
		model_statuses: [],
	};
	api.getLlmConfig.mockImplementation(async () => clone(serverPool));
	api.getLlmSyncStatus.mockImplementation(async () => clone(syncStatus));
	api.getPresetCatalog.mockImplementation(async () => []);
	api.getModelCatalogUi.mockImplementation(async () => ({
		api_key_models: {},
		subscription_models: {},
		default_models: {},
	}));
	api.saveLlmPool.mockImplementation(async (models, preset) => {
		serverPool.models = clone(models);
		serverPool.preset = preset || "";
		return {};
	});
	// disconnect_llm empties the pool server-side; the reseed that follows has to
	// see that, or the editor would keep showing the models it just deleted.
	api.disconnectLlm.mockImplementation(async () => {
		serverPool.models = [];
		serverPool.preset = "";
		serverPool.proxy_active = false;
		syncStatus = { ...syncStatus, last_sync_status: "disconnected", pending: false };
		return { disconnected: true };
	});
});

afterEach(() => {
	vi.useRealTimers();
});

// Let the component's own setTimeout(0) poll tick and every awaited promise land.
async function idle() {
	for (let i = 0; i < 6; i++) {
		await flushPromises();
		await new Promise((r) => setTimeout(r, 1));
	}
	await flushPromises();
}

async function mountEditor(props = {}) {
	const w = mount(LlmPoolEditor, { props });
	await idle();
	return w;
}

const savedModels = () => api.saveLlmPool.mock.calls[api.saveLlmPool.mock.calls.length - 1][0];

describe("disconnecting an account persists (defect 1)", () => {
	it("writes the pool without the disconnected account", async () => {
		setPool([
			subModel("gpt-5", 0, [account("SUB_a", "a@x.com"), account("SUB_b", "b@x.com")]),
			keyModel("openai", "gpt-4o", 1),
		]);
		const w = await mountEditor();
		expect(w.vm.rows[0].accounts).toHaveLength(2);

		await w.vm.removeAccount(w.vm.rows[0], 1);
		await idle();

		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
		const sub = savedModels().find((m) => m.subscription);
		expect(sub.subscription.accounts).toHaveLength(1);
		expect(sub.subscription.accounts[0].account_ref).toBe("SUB_a");
		// And the reseed from the server agrees, which is the half that used to lie.
		expect(w.vm.rows[0].accounts).toHaveLength(1);
	});

	it("puts the chip back when nothing could be written", async () => {
		setPool([
			subModel("gpt-5", 0, [account("SUB_a", "a@x.com"), account("SUB_b", "b@x.com")]),
			keyModel("openai", "gpt-4o", 1),
		]);
		api.saveLlmPool.mockRejectedValue(new Error("fleet unreachable"));
		const w = await mountEditor();

		await w.vm.removeAccount(w.vm.rows[0], 1);
		await idle();

		expect(w.vm.rows[0].accounts).toHaveLength(2);
		expect(w.vm.applyResult.kind).toBe("failed");
	});

	it("does nothing without a confirmation", async () => {
		setPool([
			subModel("gpt-5", 0, [account("SUB_a", "a@x.com"), account("SUB_b", "b@x.com")]),
			keyModel("openai", "gpt-4o", 1),
		]);
		answer.confirm = false;
		const w = await mountEditor();

		await w.vm.removeAccount(w.vm.rows[0], 1);
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
		expect(w.vm.rows[0].accounts).toHaveLength(2);
	});

	it("removes the model too when its last account goes, and says so first", async () => {
		setPool([
			subModel("gpt-5", 0, [account("SUB_a", "a@x.com")]),
			keyModel("openai", "gpt-4o", 1),
		]);
		const w = await mountEditor();

		await w.vm.removeAccount(w.vm.rows[0], 0);
		await idle();

		// An accountless subscription row cannot answer a turn, so it leaves the pool
		// with its last account rather than being written back unusable.
		expect(savedModels()).toHaveLength(1);
		expect(savedModels()[0].subscription).toBeUndefined();
		expect(w.vm.rows).toHaveLength(1);
	});

	it("refuses to disconnect the only account of the only model", async () => {
		setPool([subModel("gpt-5", 0, [account("SUB_a", "a@x.com")])]);
		const w = await mountEditor();

		await w.vm.removeAccount(w.vm.rows[0], 0);
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
		expect(w.vm.rows[0].accounts).toHaveLength(1);
		expect(w.vm.applyResult.kind).toBe("failed");
		expect(w.vm.applyResult.text).toMatch(/only model/i);
	});

	it("stays local in onboarding, where the wizard footer owns the save", async () => {
		setPool([subModel("gpt-5", 0, [account("SUB_a", "a@x.com")])]);
		const w = await mountEditor({ modes: ["quick"], footerless: true });

		await w.vm.removeAccount(w.vm.rows[0], 0);
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
		expect(w.vm.rows[0].accounts).toHaveLength(0);
	});
});

describe("an in-progress add survives an unrelated apply (defect 2)", () => {
	it("keeps a fresh, unconnected subscription row and its open panel", async () => {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		const w = await mountEditor();

		w.vm.openAdd();
		const addUid = w.vm.panel.uid;
		expect(w.vm.rows).toHaveLength(3);

		await w.vm.remove(0);
		await idle();

		// The unrelated Remove saved ONLY the real models...
		expect(savedModels()).toHaveLength(1);
		expect(savedModels()[0].model).toBe("claude-sonnet-4");
		// ...and the half-started row (plus the panel sitting on it) is still there.
		expect(w.vm.rows.some((r) => r._uid === addUid)).toBe(true);
		expect(w.vm.panel.open).toBe(true);
		expect(w.vm.panel.uid).toBe(addUid);
	});

	it("keeps a half-typed API-key row without saving it unfinished", async () => {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		const w = await mountEditor();

		w.vm.openAdd();
		w.vm.setPanelSource("api_key");
		const addUid = w.vm.panel.uid;
		const draft = w.vm.rows.find((r) => r._uid === addUid);
		draft.provider = "OpenAI";
		draft.model = "gpt-4o-mini";
		draft.apiKey = "sk-still-being-typed";

		await w.vm.remove(0);
		await idle();

		// A row nobody pressed Connect on is not a model, so it is not in the payload
		// (it is also not empty, so pruning alone would have saved it).
		expect(savedModels().map((m) => m.model)).toEqual(["claude-sonnet-4"]);
		const kept = w.vm.rows.find((r) => r._uid === addUid);
		expect(kept).toBeTruthy();
		expect(kept.apiKey).toBe("sk-still-being-typed");
		expect(w.vm.panel.open).toBe(true);
	});

	it("does not let a half-typed row count as the spare model", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();

		w.vm.openAdd();
		w.vm.setPanelSource("api_key");
		const draft = w.vm.rows.find((r) => r._uid === w.vm.panel.uid);
		draft.provider = "OpenAI";
		draft.model = "gpt-4o-mini";
		draft.apiKey = "sk-still-being-typed";

		await w.vm.remove(0);
		await idle();

		// Still the LAST model, so this is a disconnect - a half-typed row nobody
		// pressed Connect on is not the spare that would have made it a plain Remove.
		expect(api.saveLlmPool).not.toHaveBeenCalled();
		expect(api.disconnectLlm).toHaveBeenCalledTimes(1);
	});

	it("still sends a row whose write already landed", async () => {
		// A Connect that persisted and then failed to APPLY leaves the add panel open on
		// a row the server already holds. That row must not be held back afterwards, or
		// the next unrelated apply would silently delete a model the customer connected.
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		syncStatus = { ...syncStatus, last_sync_status: "failed: fleet returned 502" };
		// Connect probes the key live before it writes anything; the provider accepts.
		api.testLlmApiKey.mockResolvedValue({ ok: true, checks: [{ detail: "accepted" }] });
		const w = await mountEditor();

		w.vm.openAdd();
		w.vm.setPanelSource("api_key");
		const addUid = w.vm.panel.uid;
		const draft = w.vm.rows.find((r) => r._uid === addUid);
		draft.provider = "OpenAI";
		draft.model = "gpt-4o-mini";
		draft.apiKey = "sk-live";
		draft.hasKey = false;
		// Let the panel's field watcher settle before Connect: it invalidates any Test
		// verdict in flight, and would discard this one as stale mid-probe.
		await flushPromises();

		// Connect from the row itself: the write lands, the apply then fails.
		await w.vm.connectApiKeyRow(draft);
		await idle();
		expect(w.vm.applyResult.kind).toBe("failed");
		expect(savedModels().map((m) => m.model)).toContain("gpt-4o-mini");

		syncStatus = { ...syncStatus, last_sync_status: "ok (restart via admin)" };
		api.saveLlmPool.mockClear();
		await w.vm.remove(0);
		await idle();

		expect(savedModels().map((m) => m.model)).toContain("gpt-4o-mini");
	});
});

describe("removing the last model disconnects the workspace", () => {
	it("calls disconnect instead of saving a pool the server would refuse", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();

		await w.vm.remove(0);
		await idle();

		expect(api.disconnectLlm).toHaveBeenCalledTimes(1);
		// save_llm_pool rejects an empty list, so reaching it at all would be the bug.
		expect(api.saveLlmPool).not.toHaveBeenCalled();
		expect(w.vm.applyResult.kind).toBe("ok");
		expect(w.vm.applyResult.text).toMatch(/^Disconnected/);
	});

	it("clears the list instead of leaving the deleted model on screen", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();

		await w.vm.remove(0);
		await idle();

		// The whole point of Disconnect: the model is gone, so the list is empty and
		// the template's "No models yet. Add one below." empty state takes over. A
		// blank placeholder row here would render as a row numbered "1" carrying no
		// provider and no model, which reads as though something were still connected.
		expect(w.vm.rows).toHaveLength(0);
		// An empty pool is the SAVED state, not unsaved work. While it read as dirty,
		// accountHealth flipped every pill to "Pending re-check" on an empty pool.
		expect(w.vm.dirty).toBe(false);
	});

	it("still clears the list when the reload after the disconnect fails", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();
		// The disconnect itself succeeds; only the refetch that follows it falls over.
		// This is the exact shape of the original defect: load() aborts inside its
		// catch before reassigning rows, so the just-deleted model survived on screen
		// underneath the "Disconnected" banner, pill reading "Pending re-check".
		api.getLlmConfig.mockRejectedValueOnce(new Error("network"));

		await w.vm.remove(0);
		await idle();

		expect(api.disconnectLlm).toHaveBeenCalledTimes(1);
		expect(w.vm.rows).toHaveLength(0);
	});

	it("keeps Remove as an ordinary edit while another model is left", async () => {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		const w = await mountEditor();

		await w.vm.remove(0);
		await idle();

		expect(api.disconnectLlm).not.toHaveBeenCalled();
		expect(savedModels().map((m) => m.model)).toEqual(["claude-sonnet-4"]);
	});

	it("labels the action for what it will do before it is pressed", async () => {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		const w = await mountEditor();
		expect(w.vm.isLastConnectedRow(w.vm.rows[0])).toBe(false);

		setPool([keyModel("openai", "gpt-4o", 0)]);
		const only = await mountEditor();
		expect(only.vm.isLastConnectedRow(only.vm.rows[0])).toBe(true);
	});

	it("does nothing without a confirmation", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		answer.confirm = false;
		const w = await mountEditor();

		await w.vm.remove(0);
		await idle();

		expect(api.disconnectLlm).not.toHaveBeenCalled();
		expect(w.vm.rows).toHaveLength(1);
	});

	it("reports a failed disconnect instead of claiming the keys are gone", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		api.disconnectLlm.mockRejectedValue(new Error("admin unreachable"));
		const w = await mountEditor();

		await w.vm.remove(0);
		await idle();

		expect(w.vm.applyResult.kind).toBe("failed");
		expect(w.vm.busy.active).toBe(false);
		// The pool is untouched on screen because it is untouched on the server.
		expect(w.vm.rows[0].model).toBe("gpt-4o");
	});

	it("is never offered in onboarding, which has nothing to disconnect from", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor({ modes: ["quick"], footerless: true });
		expect(w.vm.isLastConnectedRow(w.vm.rows[0])).toBe(false);

		await w.vm.remove(0);
		await idle();

		expect(api.disconnectLlm).not.toHaveBeenCalled();
		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});
});

describe("the status keeps updating past the blocking wait (defect 3)", () => {
	const APPLY_TIMEOUT_MS = 90000;
	const BG_POLL_MS = 15000;
	const BG_POLL_MAX_MS = 600000;

	async function slowApply() {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		syncStatus = { ...syncStatus, last_sync_status: "pending: provisioning", pending: true };
		vi.useFakeTimers();
		const w = mount(LlmPoolEditor);
		await vi.advanceTimersByTimeAsync(50);
		w.vm.move(0, 1);
		const done = w.vm.applyOrder();
		await vi.advanceTimersByTimeAsync(APPLY_TIMEOUT_MS + 1000);
		await done;
		return w;
	}

	it("releases at the deadline, then reports the real outcome without a reopen", async () => {
		const w = await slowApply();
		expect(w.vm.applyResult.text).toMatch(/^Still applying/);
		expect(w.vm.busy.active).toBe(false);

		const before = api.getLlmSyncStatus.mock.calls.length;
		syncStatus = { ...syncStatus, last_sync_status: "ok (restart via admin)", pending: false };
		await vi.advanceTimersByTimeAsync(BG_POLL_MS + 100);

		expect(w.vm.applyResult.text).toBe("Applied. Your agent is using it now.");
		// Exactly one poller: a stacked second watch would have read the status twice.
		expect(api.getLlmSyncStatus.mock.calls.length).toBe(before + 1);

		// ...and it stops once it has an answer.
		const after = api.getLlmSyncStatus.mock.calls.length;
		await vi.advanceTimersByTimeAsync(BG_POLL_MS * 4);
		expect(api.getLlmSyncStatus.mock.calls.length).toBe(after);
	});

	it("gives up on an apply that never settles instead of polling forever", async () => {
		const w = await slowApply();

		await vi.advanceTimersByTimeAsync(BG_POLL_MAX_MS + BG_POLL_MS);
		const bounded = api.getLlmSyncStatus.mock.calls.length;
		await vi.advanceTimersByTimeAsync(BG_POLL_MS * 10);

		expect(api.getLlmSyncStatus.mock.calls.length).toBe(bounded);
		expect(w.vm.applyResult.text).toMatch(/^Still applying/);
	});

	it("stops watching when the pane goes away", async () => {
		const w = await slowApply();
		w.unmount();

		const before = api.getLlmSyncStatus.mock.calls.length;
		await vi.advanceTimersByTimeAsync(BG_POLL_MS * 3);
		expect(api.getLlmSyncStatus.mock.calls.length).toBe(before);
	});
});

/**
 * PROVIDER_DEFAULTS is the local fallback for "which api-key model does this
 * provider preselect", used only while the admin catalog fetch is in flight or
 * after it fails. It is a hand-maintained mirror of the catalog's api_key
 * is_default, and nothing enforces the pairing: the 2026-07-26 catalog refresh
 * moved six ids and left this copy behind, so a customer whose catalog fetch
 * failed got preselected onto two vendor-DEPRECATED ids (deepseek-chat, retired
 * 2026-07-24, and llama-3.3-70b-versatile, retired 2026-06-17).
 *
 * These lock the fallback to the catalog. If a future refresh moves an id in
 * jarvis/_model_catalog.py without moving it here, this fails instead of
 * silently shipping a dead default.
 */
describe("api-key model defaults survive a failed catalog fetch", () => {
	// The six the 2026-07-26 refresh stranded, plus the ones that were already
	// correct, so the whole table is covered rather than just the regression.
	const EXPECTED = {
		OpenAI: "gpt-5.6",
		Anthropic: "claude-sonnet-5",
		"Google Gemini": "gemini-3.6-flash",
		Mistral: "mistral-large-latest",
		Groq: "openai/gpt-oss-120b",
		"Together AI": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
		DeepSeek: "deepseek-v4-flash",
		"Moonshot (Kimi)": "kimi-k2.6",
		"xAI Grok": "grok-4.5",
		"GLM / Z.ai": "glm-4.7",
		"GLM / Z.ai (Coding Plan)": "glm-4.7",
		OpenRouter: "anthropic/claude-sonnet-4-6",
		"Ollama (local)": "llama3",
	};

	it("falls back to the catalog's current api_key default for every provider", async () => {
		api.getModelCatalogUi.mockRejectedValue(new Error("admin unreachable"));
		const w = await mountEditor();

		for (const [label, model] of Object.entries(EXPECTED)) {
			expect(`${label}=${w.vm.providerDefaultModel(label)}`).toBe(`${label}=${model}`);
		}
	});

	it("never falls back to a vendor-deprecated id", async () => {
		api.getModelCatalogUi.mockRejectedValue(new Error("admin unreachable"));
		const w = await mountEditor();

		expect(w.vm.providerDefaultModel("DeepSeek")).not.toBe("deepseek-chat");
		expect(w.vm.providerDefaultModel("Groq")).not.toBe("llama-3.3-70b-versatile");
	});

	it("clears the model for providers that have no default", async () => {
		api.getModelCatalogUi.mockRejectedValue(new Error("admin unreachable"));
		const w = await mountEditor();

		expect(w.vm.providerDefaultModel("vLLM (local)")).toBe("");
		expect(w.vm.providerDefaultModel("OpenAI-Compatible")).toBe("");
	});

	it("snaps a row's model to the fallback when the provider is switched", async () => {
		api.getModelCatalogUi.mockRejectedValue(new Error("admin unreachable"));
		const w = await mountEditor();
		const row = w.vm.rows[0] || (w.vm.rows.push(w.vm.newRow?.() ?? {}), w.vm.rows[0]);

		w.vm.onProviderChange(row, "DeepSeek");
		expect(row.model).toBe("deepseek-v4-flash");
		expect(row.baseUrl).toBe("https://api.deepseek.com");

		w.vm.onProviderChange(row, "Groq");
		expect(row.model).toBe("openai/gpt-oss-120b");
	});

	it("prefers the fetched catalog over the local literal", async () => {
		// The literal is only a stand-in. When admin answers, admin wins - that is
		// what lets an operator add a model in the desk with no deploy.
		api.getModelCatalogUi.mockResolvedValue({
			api_key_models: {
				DeepSeek: [{ model_id: "deepseek-v9-future", label: "", is_default: true }],
			},
			subscription_models: {},
			default_models: {},
		});
		const w = await mountEditor();

		expect(w.vm.providerDefaultModel("DeepSeek")).toBe("deepseek-v9-future");
		// A provider the catalog did not mention still uses the literal.
		expect(w.vm.providerDefaultModel("Groq")).toBe("openai/gpt-oss-120b");
	});
});

// jarvis#714: the "Last sync failed" pill had no retry. resync() re-pushes the
// unchanged pool through the same save_llm_pool round trip applyOrder already
// uses for an order-only change - jarvis_settings.py's _pool_sync_is_redundant
// treats an unchanged re-save after a failed sync as the retry lever, so this
// is wiring to an existing endpoint, not new backend behaviour.
describe("resync retries a failed sync (jarvis#714)", () => {
	it("re-sends the unchanged pool and clears the failed pill on success", async () => {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		syncStatus = { ...syncStatus, last_sync_status: "failed: fleet returned 502" };
		const w = await mountEditor();

		expect(w.vm.statusLine.kind).toBe("failed");

		syncStatus = { ...syncStatus, last_sync_status: "ok (restart via admin)" };
		await w.vm.resync();
		await idle();

		expect(api.saveLlmPool).toHaveBeenCalledTimes(1);
		expect(savedModels().map((m) => m.model)).toEqual(["gpt-4o"]);
		expect(w.vm.statusLine.kind).toBe("ok");
	});

	it("does nothing while a reorder is still unapplied", async () => {
		setPool([keyModel("openai", "gpt-4o", 0), keyModel("anthropic", "claude-sonnet-4", 1)]);
		syncStatus = { ...syncStatus, last_sync_status: "failed: fleet returned 502" };
		const w = await mountEditor();

		w.vm.move(0, 1);
		expect(w.vm.orderDirty).toBe(true);
		await w.vm.resync();
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});

	it("does nothing while the add/edit panel is open", async () => {
		// A resync mid-edit would submit whatever is half-typed in the open panel
		// instead of leaving it for the customer to finish (review round 1: the
		// orderDirty guard above had a test, this sibling guard did not).
		setPool([keyModel("openai", "gpt-4o", 0)]);
		syncStatus = { ...syncStatus, last_sync_status: "failed: fleet returned 502" };
		const w = await mountEditor();

		w.vm.openAdd();
		expect(w.vm.panel.open).toBe(true);
		await w.vm.resync();
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});
});

/**
 * The Test button on an API-key row: which credential it may use (#679) and
 * what it is allowed to call a failure (#680).
 *
 * Both are driven through the real component - mount, open the real edit panel,
 * call the real click handler - because both defects are about what the editor
 * decides, not about what the probe endpoint returns. keyModel() already builds
 * the exact production state #679 lives in: has_key true from the server, and
 * therefore apiKey "" in the row, since get_llm_config never returns a secret.
 */
describe("Test button: stored key (#679)", () => {
	async function openEditOnKeyRow() {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();
		return w;
	}

	it("is enabled on a saved row whose key was never re-typed", async () => {
		const w = await openEditOnKeyRow();
		const row = w.vm.rows[0];
		// The production state: the server said has_key, so nothing is typed.
		expect(row.hasKey).toBe(true);
		expect(row.apiKey).toBe("");

		expect(w.vm.testBlockedReason(row)).toBe("");
	});

	it("tests a changed base URL against the stored key, without it in the request", async () => {
		const w = await openEditOnKeyRow();
		const row = w.vm.rows[0];
		row.baseUrl = "https://gateway.example.com/v1"; // the only edit, per #679
		await idle();
		api.testLlmApiKey.mockResolvedValue({ ok: true, verdict: "pass", checks: [], caveat: "" });

		await w.vm.testApiKeyRow(row);
		await idle();

		const sent = api.testLlmApiKey.mock.calls[0][0];
		expect(sent.base_url).toBe("https://gateway.example.com/v1");
		// The server loads the key itself; the browser never holds or sends it.
		expect(sent.use_stored_key).toBeTruthy();
		expect(sent.api_key).toBe("");
	});

	it("still blocks a brand new row, and says why", async () => {
		setPool([]);
		const w = await mountEditor();
		w.vm.openAdd();
		const row = w.vm.rows[w.vm.rows.length - 1];
		row.credentialType = "api_key";
		row.provider = "OpenAI";
		row.model = "gpt-4o";
		await idle();

		expect(row.hasKey).toBe(false);
		expect(w.vm.testBlockedReason(row)).toBe("Enter an API key to test");
	});

	it("stops using the stored key once the provider is switched", async () => {
		// The stored key belongs to the OLD provider's credential, so it is not a
		// usable credential for the new one and the button must go back to asking.
		const w = await openEditOnKeyRow();
		const row = w.vm.rows[0];
		w.vm.onProviderChange(row, "Anthropic");
		await idle();

		expect(row.hasKey).toBe(false);
		expect(w.vm.usesStoredKey(row)).toBe(false);
		expect(w.vm.testBlockedReason(row)).toBe("Enter an API key to test");
	});
});

describe("Test button: an unreachable endpoint is not a failure (#680)", () => {
	async function runTestWith(res) {
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();
		api.testLlmApiKey.mockResolvedValue(res);
		await w.vm.testApiKeyRow(w.vm.rows[0]);
		await idle();
		return w;
	}

	it("renders a container-only endpoint neutrally, not in red", async () => {
		// The #680 repro: host.docker.internal answers 200 inside the container and
		// does not resolve from the bench. The customer must not see a blocker.
		const w = await runTestWith({
			ok: false,
			verdict: "unverified",
			checks: [
				{
					check: "probe_request",
					ok: false,
					detail: "This bench could not resolve that hostname.",
				},
			],
			caveat: "Nothing reached the provider, so this is not a verdict on your key.",
		});

		const status = w.find(".jv-status");
		expect(status.exists()).toBe(true);
		expect(status.classes()).toContain("jv-status-warn");
		expect(status.classes()).not.toContain("jv-status-bad");
		expect(status.text()).toContain("Could not test from here.");
		expect(status.text()).not.toContain("Test failed.");
	});

	it("still renders a real provider rejection in red", async () => {
		// The guard on the above: a provider that ANSWERED and said no is a real
		// failure, and softening that would trade one lie for another.
		const w = await runTestWith({
			ok: false,
			verdict: "fail",
			checks: [
				{
					check: "probe_request",
					ok: false,
					detail: "HTTP 401: Incorrect API key provided.",
				},
			],
			caveat: "",
		});

		const status = w.find(".jv-status");
		expect(status.classes()).toContain("jv-status-bad");
		expect(status.text()).toContain("Test failed.");
		expect(status.text()).toContain("Incorrect API key provided.");
	});

	it("does not let an unreachable endpoint unlock the onboarding gate", async () => {
		// singleModeCanStart requires a PASS for a typed key. "unverified" is not one:
		// a typo'd public host fails DNS exactly like a container-only host does.
		setPool([]);
		const w = await mountEditor({ modes: ["quick"] });
		const row = w.vm.rows[0];
		row.credentialType = "api_key";
		row.provider = "OpenAI";
		row.model = "gpt-4o";
		row.apiKey = "sk-typed";
		row.baseUrl = "https://api.openai.com/v1";
		await idle();
		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			verdict: "unverified",
			checks: [{ check: "probe_request", ok: false, detail: "could not resolve" }],
			caveat: "",
		});

		await w.vm.testSingleModeRow();
		await idle();

		expect(w.vm.smTest.passIdentity).toBe("");
		expect(w.vm.singleModeCanStart).toBe(false);
	});
});

/**
 * Two states that only became reachable once #679 made a stored key testable and
 * #680 added a third verdict. Both are places where the screen would contradict
 * itself: an amber "nothing is broken" note next to a save that silently refuses,
 * and a red hard failure next to an enabled Start chatting.
 */
describe("the verdict and the surrounding controls agree", () => {
	it("an unreachable endpoint does not silently cancel Connect", async () => {
		// The probe result says "saving is still the way to apply it". If Connect
		// then returns without saving and without a message, the customer is stuck
		// with no path forward at all.
		setPool([]);
		const w = await mountEditor();
		w.vm.openAdd();
		const row = w.vm.rows[w.vm.rows.length - 1];
		w.vm.setCredType(row, "api_key");
		row.provider = "OpenAI-Compatible";
		row.model = "gpt-4o";
		row.apiKey = "sk-typed";
		row.baseUrl = "https://vpn-only.example.com/v1"; // not container-only by shape
		await idle();
		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			verdict: "unverified",
			checks: [{ check: "probe_request", ok: false, detail: "could not resolve" }],
			caveat: "",
		});

		await w.vm.connectApiKeyRow(row);
		await idle();

		expect(api.saveLlmPool).toHaveBeenCalled();
	});

	it("a real rejection still cancels Connect", async () => {
		setPool([]);
		const w = await mountEditor();
		w.vm.openAdd();
		const row = w.vm.rows[w.vm.rows.length - 1];
		w.vm.setCredType(row, "api_key");
		row.provider = "OpenAI";
		row.model = "gpt-4o";
		row.apiKey = "sk-bad";
		row.baseUrl = "https://api.openai.com/v1";
		await idle();
		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			verdict: "fail",
			checks: [{ check: "probe_request", ok: false, detail: "HTTP 401" }],
			caveat: "",
		});

		await w.vm.connectApiKeyRow(row);
		await idle();

		expect(api.saveLlmPool).not.toHaveBeenCalled();
	});

	it("a failed test on a saved key blocks Start chatting", async () => {
		// The returning customer whose stored key was revoked. Before #679 this row
		// could not be tested at all, so a red result next to an enabled Start was
		// impossible; now it has to be handled.
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor({ modes: ["quick"] });
		const row = w.vm.rows[0];
		expect(row.hasKey).toBe(true);
		expect(row.apiKey).toBe("");
		expect(w.vm.singleModeCanStart).toBe(true); // stored key, nothing probed yet

		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			verdict: "fail",
			checks: [{ check: "probe_request", ok: false, detail: "HTTP 401: revoked" }],
			caveat: "",
		});
		await w.vm.testSingleModeRow();
		await idle();

		expect(w.vm.singleModeCanStart).toBe(false);
		expect(w.vm.startBlockedReason).toBe(
			"That test failed. Update the settings above to continue."
		);
	});

	it("an unreachable endpoint does not block Start chatting on a saved key", async () => {
		// The guard on the above: "unverified" is not a rejection, so it must not
		// take away a path that a stored key already earned.
		setPool([keyModel("openai", "gpt-4o", 0)]);
		const w = await mountEditor({ modes: ["quick"] });
		api.testLlmApiKey.mockResolvedValue({
			ok: false,
			verdict: "unverified",
			checks: [{ check: "probe_request", ok: false, detail: "could not resolve" }],
			caveat: "",
		});
		await w.vm.testSingleModeRow();
		await idle();

		expect(w.vm.singleModeCanStart).toBe(true);
		expect(w.vm.startBlockedReason).toBe("");
	});
});

/**
 * Two ChatGPT subscriptions used to render as identical collapsed rows: the same
 * "Subscription · OpenAI" chip and the same model id, with no way to tell which
 * account is which. accountLabel() falls all the way to a generic "Account
 * connected" for accounts with neither a label nor an email (the pre-fix accounts
 * this bug is about), so the row template needs its own per-account fallback -
 * this covers both halves: the label logic and what the row actually renders.
 */
describe("a subscription row's collapsed account identity (jarvis account-name bug)", () => {
	it("gives two label-less, email-less accounts distinct identifiers via accountLabel()", async () => {
		// Real refs are secrets.token_hex(8): lowercase hex. The fallback shows the last
		// 6 chars of the ref (lowercase, matching the server-side Kimi convention), so
		// two refs that differ within their last 6 render as distinct labels.
		setPool([
			subModel("gpt-5", 0, [
				account("SUB_aaaaaaaaabcd", ""),
				account("SUB_bbbbbbbbef01", ""),
			]),
		]);
		const w = await mountEditor();

		// accountLabel is a <script setup> binding, not part of defineExpose - reachable
		// through the component's setupState the way @vue/test-utils exposes it on `vm`.
		const [a, b] = w.vm.rows[0].accounts;
		expect(w.vm.accountLabel(a)).toBe("Account aaabcd");
		expect(w.vm.accountLabel(b)).toBe("Account bbef01");
		expect(w.vm.accountLabel(a)).not.toBe(w.vm.accountLabel(b));
	});

	it("renders two distinct account identifiers for two label-less, email-less accounts", async () => {
		setPool([
			subModel("gpt-5", 0, [
				account("SUB_aaaaaaaaabcd", ""),
				account("SUB_bbbbbbbbef01", ""),
			]),
		]);
		const w = await mountEditor();

		// 2+ accounts now expand into sub-rows instead of a "+1 more" collapsed chip -
		// see the "grouped subscription rows" describe block below for the full
		// model-row/sub-row shape this replaced.
		expect(w.find(".jv-flist-acct").exists()).toBe(false);
		const emails = w.findAll(".jv-flist-subrow-email");
		expect(emails).toHaveLength(2);
		expect(emails[0].text()).toBe("Account aaabcd");
		expect(emails[1].text()).toBe("Account bbef01");
	});

	it("shows the real email when the account has one", async () => {
		setPool([subModel("gpt-5", 0, [account("SUB_aaaaaaaaabcd", "a@example.com")])]);
		const w = await mountEditor();

		const chip = w.find(".jv-flist-acct");
		expect(chip.exists()).toBe(true);
		expect(chip.text()).toBe("a@example.com");
	});

	it("never surfaces the raw SUB_<hex> token, and shows the last 6 chars of the ref", async () => {
		setPool([subModel("gpt-5", 0, [account("SUB_aaaaaaaaabcd", "")])]);
		const w = await mountEditor();

		const chip = w.find(".jv-flist-acct");
		expect(chip.text()).toBe("Account aaabcd");
		expect(chip.text()).not.toContain("SUB_");
	});
});

/**
 * A pooled subscription's accounts as grouped sub-rows. 0/1-account rows stay the
 * plain single row unchanged (the common case); 2+ accounts expand into a model row
 * plus one sub-row per account, each with its own Disconnect wired to the same
 * removeAccount() the Edit panel's account chips already use.
 */
describe("grouped subscription rows (2+ accounts)", () => {
	it("renders exactly one row and no sub-rows for a 1-account subscription", async () => {
		setPool([subModel("gpt-5", 0, [account("SUB_a", "solo@x.com")])]);
		const w = await mountEditor();

		expect(w.findAll(".jv-flist-row")).toHaveLength(1);
		expect(w.findAll(".jv-flist-subrow")).toHaveLength(0);
		expect(w.find(".jv-flist-acct").text()).toBe("solo@x.com");
	});

	it("renders a model row plus one sub-row per account for a 2-account subscription", async () => {
		setPool([
			subModel("gpt-5", 0, [
				account("SUB_a", "devhub@aerele.in"),
				account("SUB_b", "backup@aerele.in"),
			]),
		]);
		const w = await mountEditor();

		// The model row itself: no account chip, no "+N more".
		expect(w.findAll(".jv-flist-row--grouped")).toHaveLength(1);
		expect(w.find(".jv-flist-acct").exists()).toBe(false);

		const subrows = w.findAll(".jv-flist-subrow");
		expect(subrows).toHaveLength(2);
		const emails = w.findAll(".jv-flist-subrow-email");
		expect(emails[0].text()).toBe("devhub@aerele.in");
		expect(emails[1].text()).toBe("backup@aerele.in");

		const orders = w.findAll(".jv-flist-subrow-order");
		expect(orders[0].text()).toBe("primary");
		expect(orders[1].text()).toBe("backup");
	});

	it("wires a sub-row's Disconnect to removeAccount with that account's index", async () => {
		setPool([
			subModel("gpt-5", 0, [
				account("SUB_a", "devhub@aerele.in"),
				account("SUB_b", "backup@aerele.in"),
			]),
			keyModel("openai", "gpt-4o", 1),
		]);
		const w = await mountEditor();
		const spy = vi.spyOn(w.vm, "removeAccount").mockResolvedValue();

		const disconnectBtns = w
			.findAll(".jv-flist-subrow .jv-pool-disc")
			.filter((b) => b.text() === "Disconnect");
		expect(disconnectBtns).toHaveLength(2);

		await disconnectBtns[1].trigger("click");
		expect(spy).toHaveBeenCalledTimes(1);
		expect(spy.mock.calls[0][1]).toBe(1);
		expect(spy.mock.calls[0][0]).toMatchObject({ _uid: w.vm.rows[0]._uid });
	});

	// Regression: the grouped model row briefly shipped with only Up/Down/Edit/
	// Reconnect, losing the ungrouped row's single-click whole-model Remove. Without
	// it, tearing down a 2+-account model required Disconnecting every sub-row one
	// at a time (N confirm+apply round-trips, and a partial state if the customer
	// stopped part-way). Remove and the per-account Disconnect must coexist: this
	// only asserts the model-row Remove wires to remove(i), not that it replaces
	// the sub-row Disconnect (covered above).
	it("renders a Remove on the model row that calls remove with the row index", async () => {
		setPool([
			subModel("gpt-5", 0, [
				account("SUB_a", "devhub@aerele.in"),
				account("SUB_b", "backup@aerele.in"),
			]),
			keyModel("openai", "gpt-4o", 1),
		]);
		const w = await mountEditor();
		const spy = vi.spyOn(w.vm, "remove").mockResolvedValue();

		const modelRow = w.find(".jv-flist-row--grouped");
		const removeBtn = modelRow
			.findAll(".jv-pool-disc")
			.find((b) => b.text() === "Remove" || b.text() === "Disconnect");
		expect(removeBtn).toBeTruthy();

		await removeBtn.trigger("click");
		expect(spy).toHaveBeenCalledTimes(1);
		expect(spy).toHaveBeenCalledWith(0);
	});
});
