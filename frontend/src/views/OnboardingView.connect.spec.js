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
	// jarvis#840: default all-green so every pre-existing "ready navigates"
	// assertion still holds; the preflight describe block below overrides it.
	runChatPreflight: vi.fn(async () => ({
		plugin: "ok",
		persona: "ok",
		usable: { state: "ok", detail: "" },
	})),
	checkSignupPaymentState: vi.fn(async () => ({})),
	listPlans: vi.fn(async () => []),
	reconnectAvailable: vi.fn(async () => ({})),
	startAccountReconnect: vi.fn(async () => ({})),
	checkAccountReconnect: vi.fn(async () => ({})),
	startSignup: vi.fn(async () => ({})),
	finishPayment: vi.fn(async () => ({})),
	syncConnection: vi.fn(async () => ({})),
	captureOnboardingLead: vi.fn(async () => ({ ok: true })),
	getTermsUrl: vi.fn(async () => ({ url: "" })),
	getLlmApplyOperation: vi.fn(),
	// The cutover's payment flow (plan-09) instantiates from this map at setup and
	// hydrates on mount. A benign no-signup envelope lets the view mount and reconcile
	// to the intro tour, so maybeResumeConnect (the seam these tests exercise) runs.
	onboardingPaymentApi: {
		getOnboardingState: vi.fn(async () => ({
			status: 200,
			body: {
				message: {
					ok: true,
					contract_version: 2,
					data: { code: "BENCH_NO_SIGNUP_CONTEXT" },
					context: {},
				},
			},
		})),
		startSignup: vi.fn(),
		initiateSignupPayment: vi.fn(),
		checkSignupPaymentStatus: vi.fn(),
		confirmSignupPayment: vi.fn(),
		syncConnection: vi.fn(async () => ({ synced: false })),
	},
}));
vi.mock("@/api", () => api);

const routerReplace = vi.hoisted(() => vi.fn());
vi.mock("vue-router", () => ({ useRouter: () => ({ replace: routerReplace }) }));

const forgetReadySpy = vi.hoisted(() => vi.fn());
// These specs assert the CONNECT step, so there is never a reconnect intent here:
// the landing helpers are stubbed to "no intent, keep the resumed step". Mocked
// explicitly rather than via importOriginal so the real module's graph (api.js ->
// frappe-ui) stays out of this file.
vi.mock("@/onboarding/readiness.js", () => ({
	forgetReady: forgetReadySpy,
	hasReconnectIntent: () => false,
	landingStep: ({ resumedStep }) => resumedStep,
	// Slice 4b: the reconnect STOP card's CTA routes here. The real constant is
	// "/jarvis/onboarding?reconnect=1"; pin the same literal so the click assertion
	// below is exercising the true destination, not a placeholder.
	RECONNECT_INTENT_URL: "/jarvis/onboarding?reconnect=1",
}));

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
		props: ["editable", "modes", "footerless", "hostBusy"],
		emits: ["ready", "settings-changed", "subscription-testing"],
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
				subscriptionTesting: ref(false),
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
		Checkbox: {
			name: "Checkbox",
			props: ["modelValue", "label", "id"],
			emits: ["update:modelValue"],
			template:
				'<input type="checkbox" :id="id" :checked="!!modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
		},
		ErrorMessage: {
			name: "ErrorMessage",
			props: ["message"],
			template: '<div v-if="message">{{ message }}</div>',
		},
		dayjs: () => ({ format: () => "", fromNow: () => "" }),
		toast: { error: vi.fn(), success: vi.fn() },
	};
});

import OnboardingView from "./OnboardingView.vue";

const OP_STORE_KEY = "jarvis.llm_apply.operation_id";
const IDEM_KEY = "jarvis.llm_apply.idempotency_key";

const pending = {
	operation_id: "op1",
	state: "pending",
	code: "LLM_APPLY_PENDING",
	retry_after_seconds: 0,
};
const readyStatus = {
	operation_id: "op1",
	state: "ready",
	code: "LLM_READY",
	retry_after_seconds: 0,
};
// READY, but admin says the workspace itself is not chat-ready yet (chat_readiness
// === false) - the classifyOperation branch that starts waitForChatReadiness.
const readyChatBlockedStatus = {
	operation_id: "op1",
	state: "ready",
	code: "LLM_READY",
	chat_readiness: false,
	retry_after_seconds: 0,
};
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
			.mockResolvedValueOnce({
				ok: true,
				result: opResult({ apply_operation: null, resumable: true }),
			})
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
		w.unmount();
	});

	it("a reload mid-apply (operationStore has an id) resumes follow() with NO new save", async () => {
		sessionStorage.setItem(OP_STORE_KEY, "op1");
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		const w = mount(OnboardingView); // onMounted → maybeResumeConnect
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await flushPromises();

		expect(saveMock).not.toHaveBeenCalled();
		expect(api.getLlmApplyOperation).toHaveBeenCalledWith("op1");
		expect(routerReplace).toHaveBeenCalledTimes(1);
		// Left mounted (a discarded wrapper) a stray follow-loop keeps running under
		// the shared fake-timer clock and starves a LATER test's own multi-minute
		// advance - unmount aborts its controller like every other test here does.
		w.unmount();
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
		w.unmount();
	});

	// jarvis#690: a save whose admin round-trip hard-fails (network error) at
	// EVERY poll for the whole 5-minute deadline must not tell the customer
	// "It's still finishing on its own" - nothing was ever confirmed applying.
	// The controller-level computation of `neverConfirmed` (every poll for the
	// whole deadline failing vs. at least one landing) is already pinned
	// deterministically in llmOperation.spec.js; driving that same 5-minute
	// deadline through OnboardingView's real timers here as well was flaky under
	// full-suite load (many backoff/retry ticks through vitest's fake-timer
	// microtask flushing), so this calls the host's own terminal handler
	// directly with the exact status shape the controller resolves with -
	// exercising precisely the onTerminal branch this fix added, deterministically.
	it("a save that never once reached admin gets an honest retry message, not 'still finishing'", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: true });

		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.connectMessage).toMatch(/couldn't reach/i);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		w.unmount();
	});

	// The sibling of the case above: at least ONE live poll response came back
	// (admin genuinely was converging it) before the deadline elapsed. That is a
	// real observation and the copy still reports it - but it reports it as
	// something SEEN, anchored to when it was seen, rather than as a promise that
	// the job is still running now. jarvis#709 removed "it's still finishing on
	// its own" from the readiness wait for asserting exactly that; this branch was
	// the last place the phrase survived, and it made the same unverifiable claim
	// from a past observation.
	it("reports observed progress in the past tense, never as a self-healing promise", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });

		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.connectPhase).toBe("retry");
		// It still distinguishes this case from the never-confirmed one above.
		expect(w.vm.state.connectMessage).toMatch(/was still running when we last checked/i);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		w.unmount();
	});

	// The phrase jarvis#709 deleted must not survive anywhere on this screen,
	// headline included - a short heading asserting progress is the same claim as
	// a sentence asserting it.
	it("no connect terminal renders 'still finishing' in its message or its headline", async () => {
		const w = await mountConnect();

		const terminals = [
			() => w.vm.onTerminal({ timedOut: true, neverConfirmed: true }),
			() => w.vm.onTerminal({ timedOut: true, neverConfirmed: false }),
		];
		for (const drive of terminals) {
			drive();
			expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
			expect(w.vm.state.connectTitle).not.toMatch(/still finishing setup/i);
			expect(w.vm.state.connectTitle).toBeTruthy();
		}
		w.unmount();
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

	it("a descriptor-less refusal with a cooldown is a truthful retry, NOT a readiness poll", async () => {
		// Operation path (mode:"operation"), no descriptor, not resumable, carrying a
		// rate-limit cooldown: this is a SAVE refusal, not a legacy fallback. It must
		// enter the cooldown retry state and must NOT drift into followLegacyReadiness
		// (which would silently poll isReadyForChat for ~75s). Pins the mode==="legacy"
		// branch guard in resolveAndFollow.
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({
				apply_operation: null,
				resumable: false,
				mode: "operation",
				retry_after_seconds: 30,
			}),
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		const p = w.vm.saveConnect();
		await flushPromises();
		await p;

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.retryAfter).toBe(30); // the truthful cooldown, not a spinner
		expect(api.isReadyForChat).not.toHaveBeenCalled(); // did NOT fall into the legacy poll
		expect(api.getLlmApplyOperation).not.toHaveBeenCalled(); // no operation opened
		expect(routerReplace).not.toHaveBeenCalled();
		w.unmount(); // clear the cooldown countdown interval
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

	it("a support dead-end drops the idempotency key so the next Start is fresh (F1/F8)", async () => {
		// Descriptor-less, non-resumable, no cooldown → a support dead-end. It must drop
		// the persisted idempotency key so a later Retry/Start mints a fresh one instead
		// of re-submitting a poisoned/conflicting key forever.
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({
				apply_operation: null,
				resumable: false,
				mode: "operation",
				retry_after_seconds: 0,
			}),
		});
		const w = await mountConnect();

		await w.vm.saveConnect();
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("support");
		expect(sessionStorage.getItem(IDEM_KEY)).toBe(null); // key dropped on the dead-end
		expect(routerReplace).not.toHaveBeenCalled();
	});

	it("a never-ready legacy poll is BOUNDED and ends fail-closed on Connect (no nav)", async () => {
		// Proves the LEGACY_READY_ATTEMPTS bound and that a persistently-not-ready
		// workspace lands on a Retry state rather than navigating or spinning forever.
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(30 * 2500); // cover all attempts' backoff
		await p;

		expect(api.isReadyForChat).toHaveBeenCalledTimes(30); // exactly LEGACY_READY_ATTEMPTS
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(routerReplace).not.toHaveBeenCalled();
	});

	it("the subscription leg honors readiness_budget_s: the poll bound widens to 300s, not 75s", async () => {
		// jarvis#715 step 3 leg does TWO container restarts, so its confirmed apply
		// routinely lands after the 75s default. save_llm_pool returns
		// readiness_budget_s:300 for it; the poll must run to ~120 attempts (300s /
		// 2.5s), NOT stop at 30 and show a false "not connected, retry".
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({
				apply_operation: null,
				resumable: false,
				mode: "legacy",
				readiness_budget_s: 300,
			}),
		});
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(120 * 2500); // widened 300s budget
		await p;

		expect(api.isReadyForChat).toHaveBeenCalledTimes(120); // 300s / 2.5s, not 30
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(routerReplace).not.toHaveBeenCalled();
	});

	it("a subscription apply that confirms AFTER the old 75s ceiling still navigates (the fix)", async () => {
		// The exact bug: the dual-restart apply confirms past 30 probes (75s). With the
		// widened budget the poll is still running and navigates once, instead of having
		// already shown Retry at the old ceiling.
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({
				apply_operation: null,
				resumable: false,
				mode: "legacy",
				readiness_budget_s: 300,
			}),
		});
		let calls = 0;
		api.isReadyForChat.mockImplementation(async () => {
			calls += 1;
			// Not ready through the old 75s (30-probe) ceiling; ready afterwards.
			return calls >= 45 ? { ready: true } : { ready: false, reason: "llm_provisioning" };
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe
		calls = 0;

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(120 * 2500);
		await p;

		expect(routerReplace).toHaveBeenCalledTimes(1); // navigated, no false retry
		expect(routerReplace).toHaveBeenCalledWith({ name: "Chat" });
		expect(w.vm.state.connectPhase).not.toBe("retry");
	});

	it("a THROWING isReadyForChat is not a verdict: the legacy poll stays fail-closed", async () => {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe
		api.isReadyForChat.mockRejectedValue(new Error("bench hiccup"));

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(30 * 2500);
		await p;

		// Every poll threw, so none was ever read as "ready": no navigation.
		expect(api.isReadyForChat).toHaveBeenCalledTimes(30);
		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.connectPhase).toBe("retry");
	});
});

// jarvis#708: the chat-readiness wait (waitForChatReadiness for the durable-operation
// path, followLegacyReadiness for mode:"legacy") used to hit its bound and render
// "It's still finishing on its own, so you can keep waiting or retry" unconditionally
// - a claim about admin still working the problem that the wait loop cannot see is
// true, observed false on a tenant admin could never advance. These pin the fix: the
// message says only what was actually observed, and the SAME poll ceiling that used to
// offer nothing but another blind wait now also hands off to a person (mirrors
// jarvis_admin_v2#259's checkout-shell poll ceiling: support offered at the FIRST
// exhaustion, not after N retries).
describe("jarvis#708 chat-readiness wait exhaustion: honest copy + a real exit", () => {
	it("waitForChatReadiness: quotes admin's own detail, offers support, never claims self-healing", async () => {
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "container_provisioning",
			detail: "applying your LLM configuration",
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000); // CHAT_READY_ATTEMPTS * CHAT_READY_INTERVAL_MS
		await flushPromises();

		expect(api.isReadyForChat).toHaveBeenCalledTimes(40); // exactly CHAT_READY_ATTEMPTS
		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.connectMessage).toMatch(/applying your LLM configuration/);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		expect(w.vm.state.connectSupportOffered).toBe(true);
		w.unmount();
	});

	it("waitForChatReadiness: never hearing from admin says so, not 'still finishing'", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear();
		api.isReadyForChat.mockRejectedValue(new Error("bench hiccup"));

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(api.isReadyForChat).toHaveBeenCalledTimes(40);
		expect(w.vm.state.connectMessage).toMatch(/couldn't reach your workspace/i);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		expect(w.vm.state.connectSupportOffered).toBe(true);
		w.unmount();
	});

	it("followLegacyReadiness: quotes admin's own detail and offers support at the existing ceiling", async () => {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "container_provisioning",
			detail: "applying your LLM configuration",
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(30 * 2500); // LEGACY_READY_ATTEMPTS * LEGACY_READY_INTERVAL_MS
		await p;

		expect(api.isReadyForChat).toHaveBeenCalledTimes(30);
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.connectMessage).toMatch(/applying your LLM configuration/);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		expect(w.vm.state.connectSupportOffered).toBe(true);
	});

	it("a fresh Start withdraws a previous attempt's support offer until this one exhausts too", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();
		expect(w.vm.state.connectSupportOffered).toBe(true);

		// A genuinely new attempt: canStart still true, save parks pending (never
		// terminal) so this test never has to reach a second exhaustion to prove the
		// reset happened - saveConnect clears the flag synchronously, before its own
		// first await.
		api.getLlmApplyOperation.mockResolvedValue(pending);
		w.vm.saveConnect();
		await flushPromises();

		expect(w.vm.state.connectSupportOffered).toBe(false);
		w.unmount();
	});

	it("a SUPERSEDED terminal reached AFTER a prior exhaustion withdraws the stale offer", async () => {
		// Retry re-follows the SAME operation (no fresh saveConnect), so the flag a
		// prior readiness-wait exhaustion set is still sitting on state when a later
		// poll of that operation comes back superseded - a different situation
		// ("reload and retry") that must not inherit an unrelated stale offer.
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();
		expect(w.vm.state.connectSupportOffered).toBe(true);

		w.vm.onOpUpdate({ phase: "superseded", message: "Your workspace assignment changed." });

		expect(w.vm.state.connectPhase).toBe("superseded");
		expect(w.vm.state.connectSupportOffered).toBe(false);
		w.unmount();
	});

	it("the generic support dead-end reached AFTER a prior exhaustion withdraws the stale offer", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();
		expect(w.vm.state.connectSupportOffered).toBe(true);

		w.vm.onTerminal(null); // onTerminal's own dead-end: no status at all

		expect(w.vm.state.connectPhase).toBe("support");
		expect(w.vm.state.connectSupportOffered).toBe(false);
		w.unmount();
	});
});

// jarvis#727: a customer whose only model cannot be verified reached a screen
// whose single action was Retry, re-running the exact thing that had just been
// watched fail to converge - and could not reach Settings to add another model,
// because readiness.js puts llm_pool_provisioning / llm_provisioning /
// readiness_unconfirmed in NOT_ONBOARDED_REASONS, so AppShell's gate covers
// every route but this wizard. The exit therefore has to live here. These pin
// BOTH halves of the rule: it appears where the pipeline was observed to stall
// on the chosen configuration, and it stays away where nothing was observed at
// all (offering it there would blame a model nobody examined).
describe("jarvis#727 an unverifiable model has a way out, not only a Retry", () => {
	function labels(w) {
		return w.findAll("button").map((b) => b.attributes("label") || b.text());
	}

	it("the reproduced dead end (op confirmed in flight, never finished) offers a different model", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(true);
		expect(labels(w)).toContain("Use a different model");
		w.unmount();
	});

	it("a deadline that never confirmed anything keeps Retry alone: nothing points at the model", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: true });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(false);
		expect(labels(w)).not.toContain("Use a different model");
		expect(labels(w)).toContain("Retry"); // still an exit, just an honest one
		w.unmount();
	});

	it("a readiness ceiling where admin kept answering offers the model change", async () => {
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "llm_pool_provisioning",
			detail: "applying your LLM configuration",
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(true);
		w.unmount();
	});

	// Review round. The offer used to be gated on `sawVerdict`, which means only
	// "a poll returned JSON". readiness_unconfirmed IS a well-formed 200, and
	// jarvis/account.py documents it as "admin could not be asked" - so the old
	// gate told a customer their chosen connection had failed on the strength of a
	// wait in which nothing about that connection was ever established.
	it("a ceiling where every poll answered readiness_unconfirmed does NOT offer it", async () => {
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "readiness_unconfirmed",
			retryable: true,
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(false);
		expect(labels(w)).not.toContain("Use a different model");
		expect(w.vm.state.connectSupportOffered).toBe(true);
		w.unmount();
	});

	// The judgement call, recorded: container_provisioning is admin saying "not
	// Ready" for anything that is neither Suspended nor SupportRequired, and in the
	// reproduced class of failure admin's own detail rides that code ("Still
	// verifying your OpenAI subscription"). So a named container counts.
	it("a ceiling that only ever saw container_provisioning DOES offer it", async () => {
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "container_provisioning",
			detail: "Still verifying your OpenAI subscription.",
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.connectModelChangeOffered).toBe(true);
		w.unmount();
	});

	// "At least once", never "on the last poll": ninety seconds of a named apply
	// followed by one transient unconfirmed is still a wait that watched this
	// configuration fail to converge.
	it("one late unconfirmed poll does not erase a wait that DID name the apply", async () => {
		let n = 0;
		api.isReadyForChat.mockImplementation(async () => {
			n += 1;
			return n < 40
				? { ready: false, reason: "llm_pool_provisioning" }
				: { ready: false, reason: "readiness_unconfirmed", retryable: true };
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.connectModelChangeOffered).toBe(true);
		w.unmount();
	});

	it("a readiness ceiling that never reached admin does NOT offer it", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockRejectedValue(new Error("bench hiccup"));

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(false);
		expect(w.vm.state.connectSupportOffered).toBe(true);
		w.unmount();
	});

	// The three verdicts waitPhases marks `stop`. A different model resolves none
	// of them, and authority_repair_required must show no self-service action at
	// all, so the blocked panel keeps its own shape.
	it("a stopping verdict never offers a model change", async () => {
		for (const reason of [
			"authority_repair_required",
			"subscription_suspended",
			"site_replaced",
		]) {
			const w = await mountConnect();
			w.vm.connectModelChangeOffered = true; // a stale offer from an earlier attempt
			w.vm.noteReadiness({ answered: true, reason, detail: "admin's own sentence" });
			await flushPromises();

			expect(w.vm.state.connectPhase).toBe("blocked");
			expect(w.vm.connectModelChangeOffered).toBe(false);
			expect(labels(w)).not.toContain("Use a different model");
			w.unmount();
		}
	});

	// Slice 4b (C10b): admin's chat_readiness == "ReconnectRequired" reaches the
	// connect wait as is_ready_for_chat's "reconnect_required" reason. It must be a
	// TERMINAL STOP with a Reconnect action - the honest headline + admin's own
	// reason + a primary Reconnect CTA - and NEVER the endless "bringing your setup
	// online" spinner it used to fall into (bucketed as container_provisioning).
	describe("ReconnectRequired is a terminal STOP, not the endless spinner", () => {
		const RECONNECT_REASON =
			"Your AI subscription needs reconnecting. Open Jarvis Settings and reconnect your provider to finish.";

		it("renders the reconnect STOP card with admin's reason, not the spinner", async () => {
			const w = await mountConnect();
			w.vm.connectModelChangeOffered = true; // a stale offer from an earlier attempt
			w.vm.noteReadiness({
				answered: true,
				reason: "reconnect_required",
				detail: RECONNECT_REASON,
			});
			await flushPromises();

			expect(w.vm.state.connectPhase).toBe("reconnect");
			// Not the working/finishing spinner, and not the support-only blocked card.
			expect(w.vm.state.connectPhase).not.toBe("working");
			expect(w.vm.state.connectPhase).not.toBe("blocked");
			expect(w.vm.state.connectTitle).toMatch(/reconnect/i);
			expect(w.vm.state.connectMessage).toBe(RECONNECT_REASON); // admin's own sentence, verbatim
			expect(w.vm.connectModelChangeOffered).toBe(false); // a model change fixes nothing here
			expect(labels(w)).toContain("Reconnect");
			expect(labels(w)).not.toContain("Use a different model");
			w.unmount();
		});

		it("the Reconnect CTA routes to the reconnect wizard entry", async () => {
			const realLocation = window.location;
			// jsdom's real window.location.assign is non-configurable; replace the
			// whole object (same idiom as BillingPage.spec.js).
			Object.defineProperty(window, "location", {
				configurable: true,
				value: { ...window.location, assign: vi.fn() },
			});
			try {
				const w = await mountConnect();
				w.vm.noteReadiness({
					answered: true,
					reason: "reconnect_required",
					detail: RECONNECT_REASON,
				});
				await flushPromises();

				const reconnectBtn = w
					.findAll("button")
					.find((b) => (b.attributes("label") || b.text()) === "Reconnect");
				expect(reconnectBtn).toBeTruthy();
				await reconnectBtn.trigger("click");

				expect(window.location.assign).toHaveBeenCalledWith(
					"/jarvis/onboarding?reconnect=1"
				);
				w.unmount();
			} finally {
				Object.defineProperty(window, "location", {
					configurable: true,
					value: realLocation,
				});
			}
		});

		it("stops polling the instant ReconnectRequired is seen (no 40-tick spinner)", async () => {
			api.isReadyForChat.mockResolvedValue({
				ready: false,
				reason: "reconnect_required",
				detail: RECONNECT_REASON,
			});
			vi.useFakeTimers();
			const w = await mountConnect();
			api.isReadyForChat.mockClear();

			// READY-but-not-chat-ready starts waitForChatReadiness; its first poll sees
			// the stop verdict and the loop must end rather than count down 40 ticks.
			w.vm.onTerminal(readyChatBlockedStatus);
			await flushPromises();
			await vi.advanceTimersByTimeAsync(40 * 3000);
			await flushPromises();

			expect(api.isReadyForChat.mock.calls.length).toBe(1);
			expect(w.vm.state.connectPhase).toBe("reconnect");
			w.unmount();
		});
	});

	it("an operation-level failure offers the change beside Retry, not instead of it", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "retry", message: "We hit a snag applying your AI connection." });
		await flushPromises();

		expect(w.vm.connectModelChangeOffered).toBe(true);
		expect(labels(w)).toEqual(expect.arrayContaining(["Retry", "Use a different model"]));
		w.unmount();
	});

	// The whole point: the customer lands back on their own choice, and the next
	// Start is a genuinely NEW attempt. Reusing this attempt's idempotency key
	// would have admin dedupe the new configuration straight back to the stuck
	// operation, and a surviving currentOpId would make Retry re-follow it.
	it("taking the exit returns to the editable form and starts a genuinely fresh attempt", async () => {
		// A transient apply failure: terminal (so saveConnect resolves) and, unlike
		// a rejection or a supersession, one that deliberately KEEPS the idempotency
		// key so a Retry re-follows the same operation. That is exactly the state
		// the exit has to undo.
		api.getLlmApplyOperation.mockResolvedValue({
			operation_id: "op1",
			state: "failed",
			code: "LLM_APPLY_FAILED",
			message: "We hit a snag applying your AI connection.",
			retry_after_seconds: 0,
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;
		const firstKey = saveMock.mock.calls[0][0];
		expect(firstKey).toBeTruthy();
		expect(sessionStorage.getItem(IDEM_KEY)).toBe(firstKey);
		expect(w.vm.currentOpId).toBe("op1");
		expect(w.vm.connectModelChangeOffered).toBe(true);
		sessionStorage.setItem(OP_STORE_KEY, "op1"); // as a mid-apply reload would leave it

		w.vm.chooseDifferentModel();
		await flushPromises();

		expect(w.vm.state.finishing).toBe(false); // the editor, with their choice still in it
		expect(w.vm.state.connectPhase).toBe("");
		expect(w.vm.state.connectBlockReason).toMatch(/pick a different model/i);
		expect(w.vm.connectModelChangeOffered).toBe(false);
		expect(w.vm.currentOpId).toBe("");
		expect(sessionStorage.getItem(IDEM_KEY)).toBe(null);
		expect(sessionStorage.getItem(OP_STORE_KEY)).toBe(null);

		const p2 = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p2;
		expect(saveMock.mock.calls[1][0]).not.toBe(firstKey);
		w.unmount();
	});

	// A live wait must not outlive the exit: without this, the ceiling of the wait
	// the customer just walked away from fires minutes later and yanks them back
	// out of the form.
	it("a wait still in flight stops when the customer takes the exit", async () => {
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_pool_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3 * 3000);
		await flushPromises();
		const pollsBefore = api.isReadyForChat.mock.calls.length;
		expect(pollsBefore).toBeGreaterThan(0);

		w.vm.chooseDifferentModel();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(api.isReadyForChat.mock.calls.length).toBeLessThanOrEqual(pollsBefore + 1);
		expect(w.vm.state.finishing).toBe(false);
		expect(w.vm.state.connectPhase).toBe("");
		w.unmount();
	});

	// The mode:"legacy" twin of the two tests above. Both jarvis#727 edits were
	// applied to followLegacyReadiness with a comment claiming parity, and the
	// review round showed BOTH could be reverted with the whole suite still green.
	function legacySave() {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
	}

	it("legacy: a ceiling that named the apply offers the model change", async () => {
		legacySave();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(30 * 2500);
		await p;

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(true);
		expect(labels(w)).toContain("Use a different model");
		w.unmount();
	});

	it("legacy: a ceiling that named nothing does NOT offer the model change", async () => {
		legacySave();
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "readiness_unconfirmed",
			retryable: true,
		});
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(30 * 2500);
		await p;

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.connectModelChangeOffered).toBe(false);
		w.unmount();
	});

	it("legacy: a wait still in flight stops when the customer takes the exit", async () => {
		legacySave();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear();

		w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3 * 2500);
		await flushPromises();
		const pollsBefore = api.isReadyForChat.mock.calls.length;
		expect(pollsBefore).toBeGreaterThan(0);

		w.vm.chooseDifferentModel();
		await vi.advanceTimersByTimeAsync(30 * 2500);
		await flushPromises();

		expect(api.isReadyForChat.mock.calls.length).toBeLessThanOrEqual(pollsBefore + 1);
		expect(w.vm.state.finishing).toBe(false);
		expect(w.vm.state.connectPhase).toBe("");
		w.unmount();
	});

	it("a genuinely fresh Start withdraws the offer until this attempt earns it too", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });
		expect(w.vm.connectModelChangeOffered).toBe(true);

		w.vm.saveConnect();
		await flushPromises();

		expect(w.vm.connectModelChangeOffered).toBe(false);
		w.unmount();
	});
});

// jarvis#727 part 2. The headline used to be a fixed "Setting up Jarvis" above a
// phase list jarvis#722 had already made real. waitPhases.setupHeadline owns the
// mapping and its honesty rule; these pin the WIRING - that the view feeds it the
// live phase, and that the never-observed states still render the generic line.
describe("jarvis#727 the setup headline follows the live phase", () => {
	it("names the brain phase while the apply operation is being followed", async () => {
		const w = await mountConnect();

		w.vm.saveConnect();
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("working");
		expect(w.vm.setupTitle).toBe("Giving Jarvis a brain");
		w.unmount();
	});

	it("follows admin's own reason once the readiness wait starts answering", async () => {
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "container_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.setupTitle).toBe("Bringing your setup online");
		w.unmount();
	});

	// Recorded decision, not an accident: mode:"legacy" mints NO durable apply
	// operation, and still shows the brain headline. What grounds the phase is that
	// the save for this configuration was accepted and is being applied, which is
	// equally true on both paths - and the phase ROW beneath the headline has said
	// exactly this on the legacy path since jarvis#722.
	it("legacy mode shows the brain headline too, matching the row beneath it", async () => {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.saveConnect();
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("working");
		expect(w.vm.setupTitle).toBe("Giving Jarvis a brain");
		// The row names the same subject as the headline. Which of the two apply
		// labels is showing depends on whether the first poll has landed yet (the
		// inFlight fallback before it, readinessPhase's after) - both are LLM_APPLY,
		// and it is the KIND, not the wording, that the headline is derived from.
		expect(w.vm.readinessStage.kind).toBe("llm_apply");
		expect(w.vm.readinessStage.label).toMatch(/applying your AI/i);
		w.unmount();
	});

	// Review round: a Retry re-follows the SAME operation via followDescriptor,
	// which flips back to the working screen. Without clearing the last attempt's
	// observation there, the ceiling's stale phase was re-rendered as the live one
	// - and this PR wires that phase into the h1, so the stale reading became the
	// biggest text on the screen.
	it("a Retry after a ceiling does not re-render the previous wait's phase", async () => {
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "container_provisioning" });
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();
		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.setupTitle).toBe("Bringing your setup online"); // the wait that just ended

		// Never terminal, so the re-follow parks on the working screen.
		api.getLlmApplyOperation.mockResolvedValue(pending);
		w.vm.retryConnect();
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("working");
		expect(w.vm.setupTitle).toBe("Giving Jarvis a brain");
		w.unmount();
	});

	it("claims nothing when the poll answered nothing", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockRejectedValue(new Error("bench hiccup"));

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.setupTitle).toBe("Setting up Jarvis");
		w.unmount();
	});

	it("says it is opening chat only once a ready verdict has actually navigated", async () => {
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		vi.useFakeTimers();
		const w = await mountConnect();

		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;

		expect(routerReplace).toHaveBeenCalledTimes(1);
		expect(w.vm.setupTitle).toBe("Opening your chat");
		w.unmount();
	});

	// The product owner asked twice to drop "workspace" from onboarding copy: at
	// this point the customer does not have one.
	it("no wait-screen headline or subtitle says workspace", async () => {
		const w = await mountConnect();

		w.vm.saveConnect();
		await flushPromises();
		expect(w.vm.setupTitle).not.toMatch(/workspace/i);
		expect(w.vm.state.finishSubtitle).not.toMatch(/workspace/i);

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		expect(w.vm.setupTitle).not.toMatch(/workspace/i);
		expect(w.vm.state.finishSubtitle).not.toMatch(/workspace/i);
		w.unmount();
	});
});

describe("staged readiness phases: the screen renders what the poll observed", () => {
	// Every one of these 40 polls used to be discarded except the last detail, so
	// the wait showed one fixed sentence for two minutes.
	it("an observed provisioning reason becomes the live phase line", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "container_provisioning",
			detail: "applying your LLM configuration",
		});

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.readinessStage.state).toBe("active");
		expect(w.vm.readinessStage.label).toMatch(/coming online/i);
		expect(w.vm.readinessStage.detail).toBe("applying your LLM configuration");
		w.unmount();
	});

	it("readiness_unconfirmed renders as unknown, never as an active phase", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "readiness_unconfirmed" });

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.readinessStage.state).toBe("unknown");
		expect(w.vm.readinessStage.label).not.toMatch(/coming online|applying/i);
		w.unmount();
	});

	it("a poll that throws never claims a phase", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockRejectedValue(new Error("network"));

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.readinessStage.observed).toBe(false);
		expect(w.vm.readinessStage.state).toBe("unknown");
		w.unmount();
	});

	// admin paged a human. Retrying payment or reconnecting here could make it
	// worse, so the wait must stop rather than run to a ceiling whose copy invites
	// exactly that.
	it("authority_repair_required stops the wait and blocks, quoting admin verbatim", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		const detail = "Your payment is safe. Please don't pay again while we sort this out.";
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "authority_repair_required",
			detail,
		});

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("blocked");
		expect(w.vm.state.connectMessage).toBe(detail);

		// Running out the rest of the ceiling must NOT convert it into a retry.
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();
		expect(w.vm.state.connectPhase).toBe("blocked");
		expect(routerReplace).not.toHaveBeenCalled();
		w.unmount();
	});

	// jarvis#709's behaviour, unchanged: the ceiling still offers support beside
	// Retry, and the message is still built from what the run observed.
	it("the poll ceiling still offers support and still uses the observed-only message", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "signup" });

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.connectSupportOffered).toBe(true);
		expect(w.vm.state.connectMessage).not.toMatch(/still finishing on its own/i);
		expect(w.vm.state.connectTitle).not.toMatch(/still finishing setup/i);
		w.unmount();
	});
});

// jarvis#757 review, gap 2: readinessPhase's "llm_rejected" case (waitPhases.js,
// waitPhases.test.js) was only ever exercised as a pure function. Nothing asserted
// that OnboardingView ITSELF routes an llm_rejected readiness answer back to the
// editable form and shows admin's reason - the whole customer-visible point of
// jarvis#757. These pin the routing, not just the copy: the wait must stop on the
// FIRST poll that names a rejection (never grind to the 40-poll ceiling the way an
// ordinary transient reason does), the form must become visible again
// (`state.finishing === false`, mirroring the jarvis#727 REJECTED operation path),
// and it must never offer the ceiling's Retry/support pair - retrying the exact
// config admin just refused cannot succeed.
describe("jarvis#757 llm_rejected routes back to the editable form", () => {
	it("stops the wait on the first poll, returns to the form, and shows admin's reason verbatim", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		const detail = "Your AI configuration was rejected: unknown llm_provider: 'gemini'";
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_rejected", detail });
		api.isReadyForChat.mockClear(); // ignore the one mount-time readiness probe

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();

		expect(api.isReadyForChat).toHaveBeenCalledTimes(1); // no ceiling-grinding on a rejection
		expect(w.vm.state.finishing).toBe(false); // the editable form is showing again
		expect(w.vm.state.connectPhase).toBe("rejected");
		expect(w.vm.state.connectBlockReason).toBe(detail);
		expect(routerReplace).not.toHaveBeenCalled();
		w.unmount();
	});

	it("never reaches the retry/support ceiling copy, even if the wait is left to run out", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockResolvedValue({
			ready: false,
			reason: "llm_rejected",
			detail: "Your AI configuration was rejected: provider + model required in oauth mode",
		});

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		// Running out the rest of the would-be ceiling must not convert this into a
		// retry: the loop already returned on the first poll (see the test above).
		await vi.advanceTimersByTimeAsync(40 * 3000);
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("rejected");
		expect(w.vm.state.connectPhase).not.toBe("retry");
		expect(w.vm.state.connectSupportOffered).not.toBe(true);
		w.unmount();
	});

	it("followLegacyReadiness (mode:legacy) routes the same llm_rejected answer back to the form", async () => {
		saveMock.mockResolvedValue({
			ok: true,
			result: opResult({ apply_operation: null, resumable: false, mode: "legacy" }),
		});
		const detail = "Your AI configuration was rejected: unknown llm_provider: 'gemini'";
		api.isReadyForChat.mockResolvedValue({ ready: false, reason: "llm_rejected", detail });
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockClear();

		const p = w.vm.saveConnect();
		await flushPromises();

		expect(api.isReadyForChat).toHaveBeenCalledTimes(1);
		expect(w.vm.state.finishing).toBe(false);
		expect(w.vm.state.connectPhase).toBe("rejected");
		expect(w.vm.state.connectBlockReason).toBe(detail);
		await vi.advanceTimersByTimeAsync(30 * 2500);
		await p;
		expect(w.vm.state.connectPhase).toBe("rejected"); // still not "retry" at the would-be ceiling
		w.unmount();
	});
});

// The payment-confirming screen. This is the moment right after the customer
// pays: they are sent back to the SPA and the bench asks the control plane
// whether the payment landed. Before this it rendered the marketing intro tour,
// because state.step is still the default "intro" until the mount reconcile
// resolves, and the correction for that ran only AFTER the awaits.
describe("the payment-confirming wait", () => {
	function returnFromPayPage() {
		window.history.replaceState(null, "", "/jarvis/onboarding?pay=done");
	}

	it("shows the confirming screen, not the intro tour, while the mount reconcile runs", async () => {
		returnFromPayPage();
		let release;
		api.onboardingPaymentApi.getOnboardingState.mockImplementation(
			() => new Promise((r) => (release = r))
		);

		const w = mount(OnboardingView);
		await flushPromises();

		// Mid-reconcile: the round trip has not answered yet.
		expect(w.vm.showConfirming).toBe(true);
		expect(w.vm.state.step).toBe("pay");
		// jarvis#728: the confirming screen is a single round trip, not a
		// tens-of-seconds wait, so it gets the short-wait spinner, not the
		// PaymentConfirmingArt canvas illustration (which no longer mounts here).
		expect(w.find('[aria-label="Loading"]').exists()).toBe(true);
		expect(w.find("canvas").exists()).toBe(false);

		release({
			status: 200,
			body: {
				message: {
					ok: true,
					contract_version: 2,
					data: { code: "BENCH_NO_SIGNUP_CONTEXT" },
					context: {},
				},
			},
		});
		await flushPromises();

		// Resolved: the confirming screen hands over rather than sticking.
		expect(w.vm.showConfirming).toBe(false);
		w.unmount();
	});

	it("is not shown on an ordinary mount that did not come back from checkout", async () => {
		window.history.replaceState(null, "", "/jarvis/onboarding");
		const w = mount(OnboardingView);
		await flushPromises();
		expect(w.vm.showConfirming).toBe(false);
		w.unmount();
	});

	// The regression this was written against: showConfirming was originally
	// derived from busy === "checking", which ONLY the explicit "Check payment
	// status" button sets. The post-checkout reconcile never sets it, so the
	// screen never appeared on the path it exists for.
	it("does not depend on the status-check busy flag", async () => {
		returnFromPayPage();
		let release;
		api.onboardingPaymentApi.getOnboardingState.mockImplementation(
			() => new Promise((r) => (release = r))
		);
		const w = mount(OnboardingView);
		await flushPromises();

		expect(w.vm.pay.busy).not.toBe("checking");
		expect(w.vm.showConfirming).toBe(true);

		release({
			status: 200,
			body: {
				message: {
					ok: true,
					contract_version: 2,
					data: { code: "BENCH_NO_SIGNUP_CONTEXT" },
					context: {},
				},
			},
		});
		await flushPromises();
		w.unmount();
	});
});

describe("the phase row never claims a check it has not made", () => {
	// The regression: readinessSeen was seeded {answered:false}, and the phase row
	// renders during the whole apply operation - a window strictly larger than the
	// readiness wait. So the first frame after a successful save announced "We
	// couldn't reach your workspace to check", a FAILED check, before one had been
	// attempted. That is the same false-claim class jarvis#708/#709 were about,
	// only inverted, and the old fixed-sentence screen never told that lie.
	it("shows the apply phase, not a failed check, before any readiness poll runs", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying" });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("working");
		expect(w.vm.readinessStage.label).not.toMatch(/couldn't reach|could not reach/i);
		expect(w.vm.readinessStage.observed).toBe(false);
		expect(w.vm.readinessStage.state).toBe("active");
		w.unmount();
	});

	it("a poll that genuinely answered nothing still reports the failed check", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		api.isReadyForChat.mockRejectedValue(new Error("network"));

		w.vm.onTerminal(readyChatBlockedStatus);
		await flushPromises();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.vm.readinessStage.label).toMatch(/couldn't reach/i);
		expect(w.vm.readinessStage.state).toBe("unknown");
		w.unmount();
	});
});

describe("every connect terminal carries its own headline", () => {
	// enterSaveRefusal was the one writer of connectPhase="retry" that never set
	// connectTitle, so a rate-limited save rendered the generic "We couldn't
	// confirm your setup" over a rate-limit body - or worse, a STALE headline left
	// behind by an earlier attempt, since nothing ever clears it.
	it("a rate-limited save gets a headline that matches its body", async () => {
		const w = await mountConnect();

		w.vm.enterSaveRefusal(30);

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.state.connectMessage).toMatch(/too many changes/i);
		expect(w.vm.state.connectTitle).toBeTruthy();
		expect(w.vm.state.connectTitle).not.toMatch(/couldn't confirm your setup/i);
		w.unmount();
	});

	it("a rate limit after an earlier terminal does not inherit the stale headline", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: true });
		const stale = w.vm.state.connectTitle;
		expect(stale).toBeTruthy();

		w.vm.enterSaveRefusal(30);
		expect(w.vm.state.connectTitle).not.toBe(stale);
		w.unmount();
	});
});

// jarvis#752: the apply operation's own chat_readiness_reason (fleet contract 1.23,
// force_probe) reaches this screen on every non-terminal poll, well before any
// readiness wait starts - so a subscription pool's route verdict (out of quota,
// unverified, still probing) is nameable during the ordinary "Applying"/"Finishing"
// state instead of only surfacing at a five-minute deadline. Previously onOpUpdate
// read ui.chatReadinessReason nowhere and rendered fixed copy for the whole wait.
describe("jarvis#752 the connect step shows admin's route verdict while still converging", () => {
	const QUOTA_REASON = "Your OpenAI account has reached its usage limit. It resets in 2 hours.";

	it("a live reason during Applying renders in the phase row, still as progress", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("working");
		expect(w.vm.readinessStage.detail).toBe(QUOTA_REASON);
		// Still progress, not a failure: the row stays ACTIVE (spinner), the same
		// state it would be in with no reason to show at all.
		expect(w.vm.readinessStage.state).toBe("active");
		expect(w.find(".ob-phase-detail").text()).toBe(QUOTA_REASON);
		w.unmount();
	});

	it("the same reason renders during Finishing (applied_waiting_readiness)", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "finishing", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("finishing");
		expect(w.vm.readinessStage.detail).toBe(QUOTA_REASON);
		expect(w.find(".ob-phase-detail").text()).toBe(QUOTA_REASON);
		w.unmount();
	});

	it("a later tick that names nothing blanks the row rather than keeping the earlier reason", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();
		expect(w.vm.readinessStage.detail).toBe(QUOTA_REASON);

		w.vm.onOpUpdate({ phase: "applying" });
		await flushPromises();

		expect(w.vm.readinessStage.detail).toBe("");
		w.unmount();
	});

	it("a retry terminal does not leave a stale reason on the (now hidden) phase row", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		w.vm.onOpUpdate({ phase: "retry", message: "We hit a snag applying your AI connection." });
		await flushPromises();

		expect(w.vm.state.connectPhase).toBe("retry");
		expect(w.vm.readinessStage.detail).toBe("");
		w.unmount();
	});

	it("the deadline timeout quotes the last reason this attempt heard, past tense", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });

		expect(w.vm.state.connectPhase).toBe("retry");
		// The existing sentence is preserved verbatim, not replaced.
		expect(w.vm.state.connectMessage).toMatch(/was still running when we last checked/i);
		expect(w.vm.state.connectMessage).toContain(QUOTA_REASON);
		w.unmount();
	});

	it("a reason that later cleared is not quoted at the deadline", async () => {
		// Round-1 review: the remembered reason used to ratchet to the newest
		// NON-EMPTY value, so a condition that had since resolved (a quota reset)
		// was still quoted while something else held the operation open. That is a
		// confidently wrong diagnosis at the exact moment a right one matters most.
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();
		// Admin no longer names a reason: the quota cleared, something else is slow.
		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: "" });
		await flushPromises();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });

		expect(w.vm.state.connectMessage).toMatch(/was still running when we last checked/i);
		expect(w.vm.state.connectMessage).not.toContain(QUOTA_REASON);
		w.unmount();
	});

	it("a timeout with nothing ever heard renders the plain fallback, nothing invented", async () => {
		const w = await mountConnect();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });

		expect(w.vm.state.connectMessage).toBe(
			"Setup was still running when we last checked, and it's taking longer than usual. You can keep waiting and retry."
		);
		w.unmount();
	});

	it("a never-confirmed timeout does not quote a reason: nothing was ever heard from admin", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: true });

		expect(w.vm.state.connectMessage).toMatch(/couldn't reach your AI provider/i);
		expect(w.vm.state.connectMessage).not.toContain(QUOTA_REASON);
		w.unmount();
	});

	it("a genuinely fresh Start forgets the previous attempt's last-heard reason", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();
		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });
		expect(w.vm.state.connectMessage).toContain(QUOTA_REASON);

		// A genuinely new attempt: never terminal, so it parks on the working screen
		// without needing a full follow to resolve.
		api.getLlmApplyOperation.mockResolvedValue(pending);
		w.vm.saveConnect();
		await flushPromises();

		w.vm.onTerminal({ timedOut: true, neverConfirmed: false });
		expect(w.vm.state.connectMessage).not.toContain(QUOTA_REASON);
		w.unmount();
	});
});

// 2026-08-16 connect-wait redesign: the six per-step tiles above the bar are
// gone too (they read as a duplicated row of tiles above a bar - user
// report). The connect wait now renders ONE smooth progress bar with a
// single "Step N of 6 · <step name>" caption above it, and the current
// step's one-line explanation below it, and admin's own detail sentence
// (jarvis#752/#754) below that. These tests pin the DOM shape, not pixel
// layout (jsdom does not compute CSS).
describe("connect wait bar: one smooth bar with a named-step caption", () => {
	const QUOTA_REASON = "Your OpenAI account has reached its usage limit. It resets in 2 hours.";

	it("renders one progressbar (no per-step tiles) and names the current step in the caption", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		// The old ob-phases column list, and the old per-step tiles above the
		// bar, must both be gone from the connect screen.
		expect(w.find(".ob-phases").exists()).toBe(false);
		expect(w.find(".ob-progress").findAll('[role="listitem"]')).toHaveLength(0);

		// Scoped to the wait bar: the top rail is its own StepProgress
		// (variant="steps") and must not leak into this assertion.
		const bar = w.find(".ob-progress").find('[role="progressbar"]');
		expect(bar.exists()).toBe(true);
		expect(w.text()).toContain("Step 2 of 6 · Workspace");
		// 2 of 6 steps filled: Connection done, Workspace (the current step) counts too.
		expect(bar.attributes("aria-valuenow")).toBe("33");
		w.unmount();
	});

	it("explains the current step in one line and keeps admin's detail separate", async () => {
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		// The explanation is the readiness phase's own sentence (waitPhases.js),
		// never invented copy; the admin-authored detail keeps its own line.
		expect(w.find(".ob-step-explain").text()).toBe(w.vm.readinessStage.label);
		expect(w.find(".ob-phase-detail").text()).toBe(QUOTA_REASON);
		w.unmount();
	});

	it("announces the explanation and admin's detail as ONE live region, not two", async () => {
		// Same rule the phase columns followed: separate role="status" regions
		// read as unrelated announcements, and a v-if-gated region mounts
		// already populated so its content is never announced at all.
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		const explain = w.find(".ob-step-explain");
		const detail = w.find(".ob-phase-detail");
		expect(explain.attributes("role")).toBeUndefined();
		expect(detail.attributes("role")).toBeUndefined();

		const owning = w
			.findAll('[role="status"]')
			.filter(
				(r) => r.element.contains(explain.element) && r.element.contains(detail.element)
			);
		expect(owning.length).toBe(1);
		w.unmount();
	});

	it("the bar caption counts all six steps, matching the fill fraction", async () => {
		// An earlier regression showed a "Step 2 of 3" caption over six actual
		// steps; this pins the count staying in sync with connectSteps.
		const w = await mountConnect();

		w.vm.onOpUpdate({ phase: "applying", chatReadinessReason: QUOTA_REASON });
		await flushPromises();

		expect(w.text()).toContain("Step 2 of 6");
		w.unmount();
	});
});

describe("subscription Test / Start-chatting mutual exclusion", () => {
	// LlmPoolEditor's own subscription Test button fires the same save_llm_pool ->
	// apply_operation round trip "Start chatting" does, via its OWN idempotency key
	// and poll loop (see LlmPoolEditor.connect.spec.js). The two must never run at
	// once - this checks the host's half of that guard: the editor's
	// "subscription-testing" emit disables the host's own gate, and the host feeds
	// its busy state back down as hostBusy.
	it("disables Start chatting while the editor reports a subscription Test in flight", async () => {
		const w = await mountConnect();
		expect(w.vm.subscriptionTesting).toBe(false);

		const editor = w.findComponent({ name: "LlmPoolEditor" });
		editor.vm.$emit("subscription-testing", true);
		await flushPromises();
		expect(w.vm.subscriptionTesting).toBe(true);

		editor.vm.$emit("subscription-testing", false);
		await flushPromises();
		expect(w.vm.subscriptionTesting).toBe(false);
		w.unmount();
	});

	it("passes its own saving state down to the editor as hostBusy", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		const editor = w.findComponent({ name: "LlmPoolEditor" });
		expect(editor.props("hostBusy")).toBe(false);

		api.getLlmApplyOperation.mockResolvedValue(pending);
		w.vm.saveConnect();
		await flushPromises();
		expect(editor.props("hostBusy")).toBe(true);
		w.unmount();
	});
});

describe("jarvis#840 the pre-chat preflight gate", () => {
	async function driveToReady(w) {
		api.getLlmApplyOperation.mockResolvedValue(readyStatus);
		const p = w.vm.saveConnect();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(50);
		await p;
		await flushPromises();
	}

	it("runs exactly once between ready and navigation, and all-green proceeds", async () => {
		vi.useFakeTimers();
		const w = await mountConnect();
		await driveToReady(w);
		expect(api.runChatPreflight).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledTimes(1);
		expect(routerReplace).toHaveBeenCalledWith({ name: "Chat" });
	});

	it("a credential rejection blocks navigation and restores the editable form with the provider's sentence", async () => {
		api.runChatPreflight.mockResolvedValue({
			plugin: "ok",
			persona: "ok",
			usable: {
				state: "auth",
				detail: "OpenAI API error (401): Incorrect API key provided",
			},
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		await driveToReady(w);
		expect(routerReplace).not.toHaveBeenCalled();
		expect(w.vm.state.finishing).toBe(false);
		expect(w.vm.state.connectBlockReason).toMatch(/401/);
		// A fresh attempt re-runs the preflight rather than reusing the refusal.
		expect(w.vm.preflight.done).toBe(false);
		// Review B1: the forgets are load-bearing. Admin dedupes on the
		// idempotency key and the stored operation id, so keeping either would
		// hand the next Start straight back the operation whose credential the
		// probe just refused - a permanent block. Both must be gone so the
		// customer's FIXED credential mints a fresh operation.
		expect(sessionStorage.getItem(IDEM_KEY)).toBeNull();
		expect(sessionStorage.getItem(OP_STORE_KEY)).toBeNull();
	});

	it("a provider usage limit is shown honestly and does NOT block chat", async () => {
		api.runChatPreflight.mockResolvedValue({
			plugin: "ok",
			persona: "ok",
			usable: { state: "rate_limit", detail: "429 usage_limit_reached" },
		});
		vi.useFakeTimers();
		const w = await mountConnect();
		await driveToReady(w);
		expect(w.vm.preflight.notice).toMatch(/usage limit/i);
		expect(routerReplace).not.toHaveBeenCalled(); // still inside the honest beat
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();
		expect(routerReplace).toHaveBeenCalledTimes(1);
	});

	it("unchecked rows and even a failed preflight call never block (fail open)", async () => {
		api.runChatPreflight.mockRejectedValue(new Error("admin down"));
		vi.useFakeTimers();
		const w = await mountConnect();
		await driveToReady(w);
		expect(routerReplace).toHaveBeenCalledTimes(1);
	});

	it("a stale terminal after the customer escaped to the form neither navigates nor probes", async () => {
		// PR #848 review: chooseDifferentModel resets the UI but never aborts an
		// in-flight follow; when that stale operation later resolves ready, the
		// gate must not yank the screen or bill a probe against the abandoned
		// config. finishing=false is the escape's signature.
		const w = await mountConnect();
		w.vm.state.finishing = false;
		await w.vm.navigateToChat();
		await flushPromises();
		expect(api.runChatPreflight).not.toHaveBeenCalled();
		expect(routerReplace).not.toHaveBeenCalled();
	});
});
