import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { h, ref } from "vue";

/**
 * The plan-05 D2 CONNECT transaction, host half (§10.4 controller matrix):
 * OnboardingView.saveConnect is the ONE awaited controller over the durable LLM-apply
 * operation. One Start click opens exactly one operation and navigates to Chat exactly
 * once, only on an authoritative ready. Resume, rejection, supersession, unmount and
 * the mode:"legacy" fallback are all exercised here against a mocked @/api + router.
 */

const api = vi.hoisted(() => ({
	getAccountDefaults: vi.fn(async () => ({})),
	listPaymentProviders: vi.fn(async () => ({ providers: ["razorpay"], default: "razorpay" })),
	isReadyForChat: vi.fn(async () => ({ ready: false, reason: "signup" })),
	checkSignupPaymentState: vi.fn(async () => ({})),
	listPlans: vi.fn(async () => []),
	reconnectAvailable: vi.fn(async () => ({})),
	startAccountReconnect: vi.fn(async () => ({})),
	checkAccountReconnect: vi.fn(async () => ({})),
	startSignup: vi.fn(async () => ({})),
	finishPayment: vi.fn(async () => ({})),
	syncConnection: vi.fn(async () => ({})),
	getLlmApplyOperation: vi.fn(),
}));
vi.mock("@/api", () => api);

const routerReplace = vi.hoisted(() => vi.fn());
vi.mock("vue-router", () => ({ useRouter: () => ({ replace: routerReplace }) }));

const forgetReadySpy = vi.hoisted(() => vi.fn());
vi.mock("@/onboarding/readiness.js", () => ({ forgetReady: forgetReadySpy }));

vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: false, paletteVars: {} }),
}));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));

// Heavy / canvas children the connect step would otherwise mount.
vi.mock("@/onboarding/SetupNeuralNet.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/onboarding/TourIntro.vue", () => ({ default: { template: "<div/>" } }));

// The editor is stubbed: it exposes exactly the seam the host reads (save/canStart/
// startBlockedReason) and emits ready. `saveMock` and `canStartRef` are driven per test.
const saveMock = vi.hoisted(() => vi.fn());
const canStartRef = vi.hoisted(() => ({ value: true }));
vi.mock("@/components/LlmPoolEditor.vue", () => ({
	default: {
		name: "LlmPoolEditor",
		props: ["editable", "modes", "footerless"],
		emits: ["ready", "settings-changed"],
		setup(_props, { emit, expose }) {
			emit("ready", true);
			// A real ref so the host's `poolRef.value.canStart` unwraps to a boolean, seeded
			// from the per-test holder (set before mount).
			const canStart = ref(canStartRef.value);
			expose({
				save: saveMock,
				busy: { active: false },
				canStart,
				startBlockedReason: ref("Test your API key before you continue."),
			});
			return () => h("div", "editor-stub");
		},
	},
}));

// frappe-ui ESM does not resolve under vitest; provide the three used components + call.
vi.mock("frappe-ui", () => {
	const stub = (name, tag = "button") => ({
		name,
		template: `<${tag} @click="$emit('click')"><slot/></${tag}>`,
	});
	return {
		call: vi.fn(),
		Button: stub("Button"),
		FormControl: stub("FormControl", "input"),
		FeatherIcon: stub("FeatherIcon", "span"),
		dayjs: () => ({ format: () => "", fromNow: () => "" }),
		toast: { error: vi.fn(), success: vi.fn() },
	};
});

import OnboardingView from "./OnboardingView.vue";

const OP_STORE_KEY = "jarvis.llm_apply.operation_id";
const IDEM_KEY = "jarvis.llm_apply.idempotency_key";

const pending = { operation_id: "op1", state: "pending", code: "LLM_APPLY_PENDING", retry_after_seconds: 0 };
const readyStatus = { operation_id: "op1", state: "ready", code: "LLM_READY", retry_after_seconds: 0 };
const rejectedStatus = {
	operation_id: "op1",
	state: "failed",
	code: "LLM_APPLY_REJECTED",
	message: "That AI configuration was rejected.",
	retry_after_seconds: 0,
};

function opResult(over = {}) {
	return {
		apply_operation: { ...pending },
		idempotency_key: "K",
		resumable: false,
		mode: "operation",
		retry_after_seconds: 0,
		...over,
	};
}

async function mountConnect() {
	const w = mount(OnboardingView);
	await flushPromises();
	w.vm.state.step = "connect";
	await flushPromises(); // let the editor stub mount + bind ref="poolRef"
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	canStartRef.value = true;
	saveMock.mockResolvedValue({ ok: true, result: opResult() });
	api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });
	// Default poll: never terminal, so a stray follow parks rather than hangs a test.
	api.getLlmApplyOperation.mockResolvedValue(pending);
});

afterEach(() => {
	vi.useRealTimers();
});

describe("§10.4 one Start = one save = one operation, ready navigates once", () => {
	it("a single click follows one operation to ready and navigates exactly once", async () => {
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;

		expect(saveMock).toHaveBeenCalledTimes(1);
		expect(api.getLlmApplyOperation).toHaveBeenCalledWith("op1");
		expect(forgetReadySpy).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledWith({ name: "Chat" });
	});

	it("a second click while in flight does NOT start a second operation", async () => {
		// Never terminal, so the first attempt stays in flight.
		api.getLlmApplyOperation.mockResolvedValue(pending);
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.saveConnect();
		await flushPromises();
		w.vm.saveConnect(); // in-flight: must be a no-op
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);

		expect(saveMock).toHaveBeenCalledTimes(1);
		w.unmount(); // abort the still-running follow
	});

	it("the gate refuses (no save) when the editor reports it cannot start", async () => {
		canStartRef.value = false;
		const w = await mountConnect();

		await w.vm.saveConnect();
		await flushPromises();

		expect(saveMock).not.toHaveBeenCalled();
		expect(w.vm.state.connectBlockReason).toMatch(/test/i);
		expect(routerReplace).not.toHaveBeenCalled();
	});
});

describe("§10.4 resume, resumable, terminal semantics", () => {
	it("a resumable:true result re-calls save with the SAME key, then follows the descriptor", async () => {
		saveMock
			.mockResolvedValueOnce({ ok: true, result: opResult({ apply_operation: null, resumable: true }) })
			.mockResolvedValueOnce({ ok: true, result: opResult() });
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;

		expect(saveMock).toHaveBeenCalledTimes(2);
		// Both calls carried the same idempotency key so admin dedupes.
		expect(saveMock.mock.calls[0][0]).toBe(saveMock.mock.calls[1][0]);
		expect(routerReplace).toHaveBeenCalledTimes(1);
	});

	it("a reload mid-apply (operationStore has an id) resumes follow() with NO new save", async () => {
		sessionStorage.setItem(OP_STORE_KEY, "op1");
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		mount(OnboardingView); // onMounted → maybeResumeConnect
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await flushPromises();

		expect(saveMock).not.toHaveBeenCalled();
		expect(api.getLlmApplyOperation).toHaveBeenCalledWith("op1");
		expect(routerReplace).toHaveBeenCalledTimes(1);
	});

	it("a REJECTED terminal never navigates and restores the editable form", async () => {
		api.getLlmApplyOperation.mockResolvedValue(rejectedStatus);
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;

		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.finishing).toBe(false); // back to the editable connect form
		expect(w.vm.state.connectPhase).toBe("rejected");
		expect(w.vm.state.connectBlockReason).toMatch(/rejected/i);
	});

	it("a duplicate terminal cannot navigate twice (one-shot guard)", async () => {
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;
		expect(routerReplace).toHaveBeenCalledTimes(1);

		// Re-follow the same op: it resolves ready again, but navigation is one-shot.
		w.vm.retryConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await flushPromises();
		expect(routerReplace).toHaveBeenCalledTimes(1);
	});

	it("unmounting aborts the controller and never navigates afterwards", async () => {
		api.getLlmApplyOperation.mockResolvedValue(pending); // never terminal
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(10);
		w.unmount();
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();

		expect(routerReplace).not.toHaveBeenCalled();
	});
});

describe("§10.4 mode:legacy fallback (no durable operation)", () => {
	it("falls back to a bounded readiness poll and navigates once on ready", async () => {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		api.isReadyForChat.mockResolvedValueOnce({ ready: false, reason: "signup" }); // reconcile
		api.isReadyForChat.mockResolvedValue({ ready: true }); // the legacy poll
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await p;

		expect(api.getLlmApplyOperation).not.toHaveBeenCalled(); // no operation to follow
		expect(forgetReadySpy).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledWith({ name: "Chat" });
	});
});
