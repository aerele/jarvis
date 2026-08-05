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
