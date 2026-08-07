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
}));

vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: false, paletteVars: {} }),
}));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));

// Heavy / canvas children the connect step would otherwise mount.
vi.mock("@/onboarding/SetupNeuralNet.vue", () => ({ default: { template: "<div/>" } }));
// Canvas illustration: jsdom has no 2d context, and neither spec is about the art.
vi.mock("@/onboarding/PaymentConfirmingArt.vue", () => ({ default: { template: "<div/>" } }));
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
