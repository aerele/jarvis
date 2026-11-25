import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * LlmPoolEditor: the Model dropdown on a chat-subscription row (settings editor).
 * Same mocks and fake server as LlmPoolEditor.spec.js.
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
	beginClaudeCliLogin: vi.fn(),
	completeClaudeCliLogin: vi.fn(),
	cancelClaudeCliLogin: vi.fn(),
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
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<span/>" },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="stub-badge" :data-label="label">{{ label }}</span>`,
	},
}));

const answer = vi.hoisted(() => ({ confirm: true }));
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm: async () => answer.confirm }),
	confirm: async () => answer.confirm,
	confirmState: { value: null },
	settleConfirm: () => {},
}));

import LlmPoolEditor from "./LlmPoolEditor.vue";
import { MODEL_CATALOG_UI } from "@/lib/__fixtures__/modelCatalogUi.fixtures.js";

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
	api.getModelCatalogUi.mockImplementation(async () => clone(MODEL_CATALOG_UI));
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

// A promise the test settles by hand, to hold one fetch open.
function deferred() {
	let resolve, reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

async function mountEditor(props = {}) {
	const w = mount(LlmPoolEditor, { props });
	await idle();
	return w;
}

const savedModels = () => api.saveLlmPool.mock.calls[api.saveLlmPool.mock.calls.length - 1][0];

const codex = (model = "gpt-5.6-terra") =>
	subModel(model, 0, [account("SUB_stored", "test@example.com")]);
const grok = () =>
	subModel("grok-4.3", 1, [
		{ upstream: "xai", account_ref: "XAI_stored", label: "xAI", account_email: "x@x.com" },
	]);

const field = (w) => w.find('[data-testid="sub-model-field"]');
const optionTexts = async (w) => {
	await field(w).find(".jvc-field").trigger("click");
	return field(w)
		.findAll(".jvc-opt")
		.map((o) => o.text());
};

describe("Model dropdown on a chat-subscription row", () => {
	it("lists the provider's catalog models and keeps the saved one selected", async () => {
		setPool([codex()]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();

		expect(field(w).find(".jv-pool-lab").text()).toBe("Model");
		expect(field(w).find(".jvc-val").text()).toBe("gpt-5.6-terra");
		expect(await optionTexts(w)).toEqual(MODEL_CATALOG_UI.subscription_models.OpenAI);
	});

	it("shows a saved model the catalog no longer lists, without rewriting it", async () => {
		setPool([codex("gpt-4-legacy")]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();

		expect(field(w).find(".jvc-val").text()).toBe("gpt-4-legacy");
		expect((await optionTexts(w))[0]).toBe("gpt-4-legacy");
		expect(w.vm.panelRow.model).toBe("gpt-4-legacy");
	});

	it("picking another model changes the row, dirties the pool and saves it", async () => {
		setPool([codex()]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();
		expect(w.vm.dirty).toBe(false);

		await field(w).find(".jvc-field").trigger("click");
		const sol = field(w)
			.findAll(".jvc-opt")
			.find((o) => o.text() === "gpt-5.6-sol");
		await sol.trigger("mousedown");
		await idle();

		expect(w.vm.panelRow.model).toBe("gpt-5.6-sol");
		expect(w.vm.dirty).toBe(true);
		await w.vm.runApply();
		await idle();
		expect(savedModels()[0].model).toBe("gpt-5.6-sol");
		expect(savedModels()[0].subscription.accounts[0].account_ref).toBe("SUB_stored");
	});

	it("is hidden when the provider has only one catalog model", async () => {
		api.getModelCatalogUi.mockImplementation(async () => {
			const c = clone(MODEL_CATALOG_UI);
			c.subscription_models.OpenAI = ["gpt-5.6-terra"];
			return c;
		});
		setPool([codex()]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();

		expect(field(w).exists()).toBe(false);
	});

	it("changing provider resets the model to that provider's default", async () => {
		setPool([codex("gpt-5.6-sol"), grok()]);
		const w = await mountEditor();
		w.vm.openEdit(0);
		await idle();

		w.vm.panelRow.upstream = "anthropic";
		w.vm.onUpstreamChange(w.vm.panelRow);
		await idle();

		expect(w.vm.panelRow.model).toBe("claude-opus-5");
		expect(await optionTexts(w)).toEqual(MODEL_CATALOG_UI.subscription_models.Anthropic);
	});
});
