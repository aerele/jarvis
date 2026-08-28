import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect, vi, beforeEach } from "vitest";

// readiness.js reaches the backend through @/api.js, whose real module pulls in
// frappe-ui. The factory form of vi.mock keeps that module out of the graph
// entirely, so these tests exercise the verdict logic and nothing else.
const isReadyForChat = vi.fn();
vi.mock("@/api.js", () => ({ isReadyForChat: (...a) => isReadyForChat(...a) }));

const {
	readinessDetailOf,
	needsLlmConnection,
	needsOnboarding,
	regateOnboarding,
	regateOnRouteChange,
	forgetReady,
	replacedBanner,
	hasReconnectIntent,
	landingStep,
	isLlmApplying,
	isLlmApplyStuck,
	isContainerUnavailable,
	RECONNECT_INTENT_URL,
} = await import("./readiness.js");

// The verdict is memoized for the page load, so every case has to start clean.
beforeEach(() => {
	forgetReady();
	isReadyForChat.mockReset();
});

const verdict = (v) => isReadyForChat.mockResolvedValue(v);

/**
 * ChatView renders the not-ready banners as ONE v-else-if chain, so at most one
 * of these two accessors may answer for any given reason. When both answered for
 * "llm_credentials" the generic, button-less "Chat may not work yet" banner won
 * the chain and the actionable "No AI connected" banner (the only route back to
 * the AI models pane) could never appear. These tests pin the split so a future
 * edit cannot silently shadow it again.
 */
describe("readiness verdict ownership", () => {
	it("gives llm_credentials to needsLlmConnection ALONE", async () => {
		// is_ready_for_chat sends no `detail` for this reason (jarvis/account.py):
		// there is nothing to quote, only a state to name.
		verdict({ ready: false, reason: "llm_credentials" });
		expect(await needsLlmConnection()).toBe(true);
		expect(await readinessDetailOf()).toBe("");
	});

	it("still hands llm_credentials to needsLlmConnection when a detail leaks in", async () => {
		// Defensive: readinessDetailOf must key off the REASON, not the presence of
		// a detail string, or a future backend that starts sending one for this
		// reason would re-create the shadowing bug.
		verdict({ ready: false, reason: "llm_credentials", detail: "Anything at all." });
		expect(await needsLlmConnection()).toBe(true);
		expect(await readinessDetailOf()).toBe("");
	});

	it("keeps container_provisioning on the generic banner, not the AI one", async () => {
		verdict({
			ready: false,
			reason: "container_provisioning",
			detail: "Your OpenAI account has reached its usage limit.",
		});
		expect(await readinessDetailOf()).toBe("Your OpenAI account has reached its usage limit.");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows authority_repair_required on the generic banner, never a CTA one", async () => {
		// review plan 04 P0-6: the safe blocked state must surface admin's own
		// reassurance and NOTHING that offers payment/reconnect. It rides the same
		// generic detail banner as container_provisioning, and needsLlmConnection
		// (the only accessor with a CTA) must stay silent for it.
		verdict({
			ready: false,
			reason: "authority_repair_required",
			detail: "Your payment is safe — please don't retry payment.",
		});
		expect(await readinessDetailOf()).toBe(
			"Your payment is safe — please don't retry payment."
		);
		expect(await needsLlmConnection()).toBe(false);
	});

	it("jarvis#885: surfaces container_unavailable's detail sentence, never the AI CTA", async () => {
		// A dead container is not a missing LLM credential, so needsLlmConnection
		// must stay silent for it (its dedicated banner is isContainerUnavailable(),
		// checked separately by ChatView - see the exhaustive test below for the
		// mutual-exclusion pin).
		verdict({
			ready: false,
			reason: "container_unavailable",
			detail: "Your workspace container stopped responding.",
		});
		expect(await readinessDetailOf()).toBe("Your workspace container stopped responding.");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("leaves subscription_suspended to its own Renew banner", async () => {
		verdict({ ready: false, reason: "subscription_suspended", detail: "Renew to continue." });
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows neither banner on a ready workspace", async () => {
		verdict({ ready: true, reason: null });
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows neither banner when the check throws (fail-open)", async () => {
		isReadyForChat.mockRejectedValue(new Error("admin unreachable"));
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	// The exhaustive form of the rule above: whatever account.py answers, the two
	// accessors must never BOTH claim the same verdict.
	it("never lets both accessors claim one reason", async () => {
		const reasons = [
			"signup",
			"llm_credentials",
			"llm_pool_provisioning",
			"llm_provisioning",
			"container_provisioning",
			"authority_repair_required",
			"container_unavailable",
			"subscription_suspended",
			"llm_applying",
			"llm_apply_stuck",
			null,
		];
		for (const reason of reasons) {
			forgetReady();
			verdict({ ready: false, reason, detail: "d" });
			const both = !!(await readinessDetailOf()) && (await needsLlmConnection());
			expect(both, `reason ${reason} is claimed by both accessors`).toBe(false);
		}
	});
});

/**
 * jarvis#691: AppShell reads needsOnboarding() exactly ONCE at mount (it never
 * remounts across a client-side navigation) and holds the answer in a local
 * ref, so nothing re-derives the gate on its own when a connect succeeds while
 * still on /onboarding - the customer lands on Chat with the poster still up
 * over a workspace the backend now calls ready. regateOnboarding(current) is
 * what AppShell calls on every route change to close that gap; these pin its
 * two behaviours directly, without mounting the (heavy) shell component.
 */
describe("llm_rejected still gates (jarvis#757, round-1 review of #760)", () => {
	it("keeps a never-connected customer on the onboarding gate", async () => {
		// llm_rejected fires in the SAME structural position as the two
		// provisioning reasons, a first sync that never succeeded, and it only
		// ever REPLACES one of them. Adding the reason without listing it in
		// NOT_ONBOARDED_REASONS silently reopened the gate, dropping a customer
		// whose connection was never accepted into a chat shell that cannot
		// answer them.
		forgetReady();
		verdict({ ready: false, reason: "llm_rejected", detail: "provider + model required" });
		expect(await needsOnboarding()).toBe(true);
	});
});

/**
 * jarvis#825: a stuck apply must be its own honest, retryable in-app state - NOT
 * the full-screen setup gate (that was the pre-#825 bug: an established customer
 * whose apply hung got silently bounced to the wizard), and NOT the two ownership
 * accessors above (it gets its own banner + Retry). These pin all three.
 */
describe("llm_apply_stuck (jarvis#825)", () => {
	it("isLlmApplyStuck answers ONLY for its own reason", async () => {
		verdict({ ready: false, reason: "llm_apply_stuck" });
		expect(await isLlmApplyStuck()).toBe(true);
		forgetReady();
		verdict({ ready: false, reason: "llm_applying" });
		expect(await isLlmApplyStuck()).toBe(false);
		forgetReady();
		verdict({ ready: true, reason: null });
		expect(await isLlmApplyStuck()).toBe(false);
	});

	it("isLlmApplying and isLlmApplyStuck are mutually exclusive", async () => {
		verdict({ ready: false, reason: "llm_apply_stuck" });
		expect(await isLlmApplying()).toBe(false);
		forgetReady();
		verdict({ ready: false, reason: "llm_applying" });
		expect(await isLlmApplyStuck()).toBe(false);
	});

	it("does NOT trip the full-screen onboarding gate", async () => {
		// The whole point of #825: the workspace WAS established, so it keeps its
		// chat + history and gets a banner, never the setup poster. If this ever
		// returns true, llm_apply_stuck was wrongly added to NOT_ONBOARDED_REASONS.
		verdict({ ready: false, reason: "llm_apply_stuck" });
		expect(await needsOnboarding()).toBe(false);
	});
});

/**
 * jarvis#885: a dead/unhealthy container must be its own honest, outage-framed
 * banner - NOT the generic "Chat may not work yet" title (readinessDetailOf
 * still supplies the detail SENTENCE, but isContainerUnavailable is what picks
 * the banner), and NOT the full-screen onboarding gate (an established
 * workspace whose container later died keeps its chat + history).
 */
describe("container_unavailable (jarvis#885)", () => {
	it("isContainerUnavailable answers ONLY for its own reason", async () => {
		verdict({ ready: false, reason: "container_unavailable" });
		expect(await isContainerUnavailable()).toBe(true);
		forgetReady();
		verdict({ ready: false, reason: "container_provisioning" });
		expect(await isContainerUnavailable()).toBe(false);
		forgetReady();
		verdict({ ready: true, reason: null });
		expect(await isContainerUnavailable()).toBe(false);
	});

	it("never claims the llm_credentials CTA banner", async () => {
		verdict({ ready: false, reason: "container_unavailable" });
		expect(await needsLlmConnection()).toBe(false);
	});

	it("does NOT trip the full-screen onboarding gate", async () => {
		// The workspace WAS established (it had a running container that then
		// died), so it keeps its chat + history and gets a banner, never the
		// setup poster.
		verdict({ ready: false, reason: "container_unavailable" });
		expect(await needsOnboarding()).toBe(false);
	});
});

describe("regateOnboarding (jarvis#691 stale-gate re-check)", () => {
	it("re-confirms with the backend when the caller is currently gated", async () => {
		// The memo held a stale "never onboarded" verdict (captured before an
		// in-app connect landed); forgetReady() (as navigateToChat() already
		// calls before it changes the route) clears it, so the re-check below
		// sees the fresh, ready answer.
		verdict({ ready: false, reason: "llm_setup" });
		expect(await needsOnboarding()).toBe(true);
		forgetReady();
		verdict({ ready: true, reason: null });
		expect(await regateOnboarding(true)).toBe(false);
	});

	it("does not re-check when the caller is not currently gated", async () => {
		// A false verdict never needs re-confirming (an established workspace's
		// later degrade is a soft llm_credentials banner, never a return to a
		// NOT_ONBOARDED_REASONS gate) - regateOnboarding must not spend a
		// round-trip on every ordinary navigation.
		isReadyForChat.mockRejectedValue(new Error("must not be called"));
		expect(await regateOnboarding(false)).toBe(false);
		expect(await regateOnboarding(null)).toBe(null);
		expect(isReadyForChat).not.toHaveBeenCalled();
	});

	it("stays gated when the fresh check still says not onboarded", async () => {
		verdict({ ready: false, reason: "signup" });
		expect(await regateOnboarding(true)).toBe(true);
	});
});

// A promise the test controls the settlement of, so a call to isReadyForChat
// can be held open exactly as long as a scenario needs.
function deferred() {
	let resolve;
	const promise = new Promise((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

/**
 * round-4 review F3/F4: AppShell.vue's route-change handler used to write
 * `gatedOnboarding` directly and without any sequencing guard. Both defects
 * are about TIMING, not the eventual value, so these pin the sequence of
 * states over the course of the async call rather than only its final result
 * (per the review's own instruction: "tests that pin the timing, not just the
 * end state").
 */
describe("regateOnRouteChange (jarvis#691 round-4 review F3/F4)", () => {
	it("F3: never writes the stale gated value while the re-check is in flight (no flash)", async () => {
		const d = deferred();
		isReadyForChat.mockReturnValue(d.promise);
		const gated = { value: true };
		const regating = { value: false };

		const p = regateOnRouteChange(gated, regating);
		// Synchronously, before the network call resolves: the caller (AppShell's
		// showGate) reads gated.value === true - if that gets rendered
		// unconditionally, THAT is the flash this fix exists to close. The hold
		// flag going up in the SAME microtask is what lets a caller avoid it.
		await Promise.resolve(); // let regateOnRouteChange reach its first await
		expect(regating.value).toBe(true);
		expect(gated.value).toBe(true); // untouched - neither cleared nor rewritten yet

		d.resolve({ ready: true, reason: null });
		await p;

		expect(regating.value).toBe(false);
		expect(gated.value).toBe(false); // the fresh verdict, written only now
	});

	it("F3: a workspace still not onboarded also clears the hold once confirmed, poster stays up honestly", async () => {
		const d = deferred();
		isReadyForChat.mockReturnValue(d.promise);
		const gated = { value: true };
		const regating = { value: false };

		const p = regateOnRouteChange(gated, regating);
		await Promise.resolve();
		expect(regating.value).toBe(true);

		d.resolve({ ready: false, reason: "signup" });
		await p;

		expect(regating.value).toBe(false);
		expect(gated.value).toBe(true); // confirmed still gated - the poster is correct here
	});

	it("is a fast no-op (no hold, no network call) when not currently gated", async () => {
		isReadyForChat.mockRejectedValue(new Error("must not be called"));
		const gated = { value: false };
		const regating = { value: false };

		await regateOnRouteChange(gated, regating);

		expect(regating.value).toBe(false);
		expect(gated.value).toBe(false);
		expect(isReadyForChat).not.toHaveBeenCalled();
	});

	it("F4: only the most recently ISSUED check may write, regardless of resolution order", async () => {
		const first = deferred();
		const second = deferred();
		isReadyForChat.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		const gated = { value: true };
		const regating = { value: false };

		// Two route changes in quick succession while still gated: both issue
		// before either resolves. checkReady() memoizes for the page load, so a
		// SECOND genuinely independent read needs its own forgetReady() in
		// between - exactly what happens between two real back-to-back
		// connect-driven navigations, each of which calls forgetReady() itself
		// before changing the route (see navigateToChat in OnboardingView.vue).
		const p1 = regateOnRouteChange(gated, regating);
		await Promise.resolve();
		forgetReady();
		const p2 = regateOnRouteChange(gated, regating);
		await Promise.resolve();

		// Resolve OUT OF ISSUE ORDER: the second (newer) call answers first, with
		// the fresh "ready" verdict; the first (now-stale) call answers after,
		// with a verdict that would wrongly re-gate a ready workspace if it were
		// allowed to write.
		second.resolve({ ready: true, reason: null });
		await p2;
		expect(gated.value).toBe(false); // the newer call's answer landed

		first.resolve({ ready: false, reason: "signup" });
		await p1;
		// The stale, earlier-issued call must NOT have overwritten the fresher
		// answer, even though it resolved second (last write does not win here -
		// issue order does).
		expect(gated.value).toBe(false);
	});
});

/**
 * The banner stack in ChatView is a v-else-if chain, so its SOURCE ORDER is the
 * behaviour. Mounting ChatView to assert this is not viable (it is the largest
 * component in the app and boots sockets, so this reads the template instead,
 * the same trick dashboardOpen.test.js already uses on this file).
 */
describe("ChatView banner chain order", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

	it("tests the specific No-AI banner before the generic not-ready one", () => {
		const specific = chatSrc.indexOf('v-else-if="noAiConnected"');
		const generic = chatSrc.indexOf('v-else-if="notReadyNotice"');
		expect(specific, "ChatView must still render the noAiConnected banner").not.toBe(-1);
		expect(generic, "ChatView must still render the notReadyNotice banner").not.toBe(-1);
		// Specific before generic: the No-AI banner carries the "Connect a model"
		// call to action, so a generic banner ahead of it in the chain leaves a
		// disconnected customer with no discoverable way back to the AI models pane.
		expect(specific).toBeLessThan(generic);
	});

	it("orders the stuck-apply banner AFTER the still-converging one (jarvis#825)", () => {
		const applying = chatSrc.indexOf('v-else-if="llmApplying"');
		const stuck = chatSrc.indexOf('v-else-if="llmApplyStuck"');
		expect(applying, "ChatView must still render the llmApplying banner").not.toBe(-1);
		expect(stuck, "ChatView must render the llmApplyStuck banner").not.toBe(-1);
		// llmApplying is the LIVE, still-converging window; llmApplyStuck is the
		// aged-out one. Applying must win the chain while it is true, so the honest
		// "didn't finish" banner only shows once the soft window has genuinely
		// lapsed - never over a retry that just flipped the reason back to applying.
		expect(applying).toBeLessThan(stuck);
	});

	it("orders the no-workers banner AFTER suspendedNotice (billing takes precedence, jarvis Task 8)", () => {
		const suspended = chatSrc.indexOf('v-else-if="suspendedNotice"');
		const workers = chatSrc.indexOf('v-else-if="workersNotice"');
		expect(suspended, "ChatView must still render the suspendedNotice banner").not.toBe(-1);
		expect(workers, "ChatView must render the workersNotice banner").not.toBe(-1);
		expect(suspended).toBeLessThan(workers);
	});

	it("orders the soft workersWarnNotice banner AFTER the hard workersNotice block", () => {
		// worker_blocked implies degraded, so the hard block must win the
		// v-else-if race whenever both are true - the soft warning below it in
		// the chain then only ever shows on its own.
		const workers = chatSrc.indexOf('v-else-if="workersNotice"');
		const warn = chatSrc.indexOf('v-else-if="workersWarnNotice"');
		expect(workers, "ChatView must still render the workersNotice banner").not.toBe(-1);
		expect(warn, "ChatView must render the workersWarnNotice banner").not.toBe(-1);
		expect(workers).toBeLessThan(warn);
	});
});

/**
 * jarvis Task 8: insufficient_workers must be a PERSISTENT but SELF-HEALING
 * banner - unlike suspendedNotice (whose only exit is renewal), a dead worker
 * lane can come back on its own, and checkReady() is memoized (never re-polls),
 * so the banner cannot wait on a fresh readiness check to clear. Mounting
 * ChatView / driving send() is not viable here (same reasoning as the banner
 * chain order block above and dashboardOpen.test.js), so this pins the wiring
 * by source, the established technique for this SFC.
 */
describe("workersNotice self-heal (jarvis Task 8)", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

	it("raises the persistent banner when retry() is rejected for insufficient_workers", () => {
		// retry() (~line 8234) sits earlier in the file than send() (~line 8467), so
		// this is the FIRST occurrence of the reason check.
		const idx = chatSrc.indexOf('r.reason === "insufficient_workers"');
		expect(idx, "retry() must check r.reason === insufficient_workers").not.toBe(-1);
		const nearby = chatSrc.slice(idx, idx + 320);
		// Sets the persistent banner ref directly, not a toast that would vanish
		// before the customer can act on it.
		expect(nearby).toContain("workersNotice.value = WORKERS_BLOCKED_MSG");
	});

	it("mirrors the same insufficient_workers handling in send()", () => {
		const first = chatSrc.indexOf('r.reason === "insufficient_workers"');
		const second = chatSrc.indexOf('r.reason === "insufficient_workers"', first + 1);
		expect(second, "send() must also check r.reason === insufficient_workers").not.toBe(-1);
		expect(chatSrc.slice(second, second + 200)).toContain(
			"workersNotice.value = WORKERS_BLOCKED_MSG"
		);
	});

	it("clears the banner on the next successful retry (self-heal, no reload)", () => {
		// retry() sits earlier in the file than send(), so this is the FIRST
		// occurrence of the clear.
		const fnStart = chatSrc.indexOf("async function retry(messageId)");
		const fnEnd = chatSrc.indexOf("\nasync function send(", fnStart);
		expect(fnStart, "ChatView must still define retry()").not.toBe(-1);
		expect(fnEnd, "ChatView must still define send() after retry()").not.toBe(-1);
		const body = chatSrc.slice(fnStart, fnEnd);
		const awaitIdx = body.indexOf("await api.retryMessage(messageId)");
		const idx = body.indexOf("workersNotice.value = null");
		expect(awaitIdx, "retry() must still await api.retryMessage").not.toBe(-1);
		expect(idx, "retry() must clear workersNotice on a successful response").not.toBe(-1);
		// Only AFTER the await (never optimistically, before the server has
		// actually answered) and gated on r.ok !== false (a genuinely accepted
		// retry), not folded into the rejection branch that raises the banner.
		expect(idx).toBeGreaterThan(awaitIdx);
		const before = body.slice(Math.max(0, idx - 120), idx);
		expect(before).toContain("r.ok !== false");
	});

	it("also clears the banner on the next successful send (mirrors retry())", () => {
		const first = chatSrc.indexOf("workersNotice.value = null");
		const second = chatSrc.indexOf("workersNotice.value = null", first + 1);
		expect(second, "send() must also clear workersNotice on a successful response").not.toBe(
			-1
		);
		const before = chatSrc.slice(Math.max(0, second - 120), second);
		expect(before).toContain("r.ok !== false");
	});

	it("never gates canSend on workersNotice (composer must stay enabled to self-heal)", () => {
		// A disabled composer paired with a memoized (never re-polling) checkReady()
		// could never recover without a full page reload, contradicting the banner's
		// own "try again shortly" copy - the server gate, not the client, is the
		// authority on whether a send is allowed through.
		const start = chatSrc.indexOf("const canSend = computed(");
		expect(start, "ChatView must still define canSend").not.toBe(-1);
		const end = chatSrc.indexOf("\n);", start);
		expect(chatSrc.slice(start, end)).not.toContain("workersNotice");
	});

	it("seeds workersNotice from the boot readiness poll's worker_blocked field", () => {
		const idx = chatSrc.indexOf("r && r.worker_blocked");
		expect(idx, "the boot checkReady().then() block must read r.worker_blocked").not.toBe(-1);
		expect(chatSrc.slice(idx, idx + 80)).toContain("WORKERS_BLOCKED_MSG");
	});
});

/**
 * Soft, non-blocking counterpart to workersNotice above: is_ready_for_chat's
 * worker_warning fires for degraded/under-provisioned workers, distinct from
 * the hard worker_blocked zero-workers state. Same source-proximity technique
 * as the workersNotice suite (mounting ChatView is not viable here either).
 */
describe("workersWarnNotice self-heal (round 2 mainchat warning)", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

	it("seeds workersWarnNotice from the boot readiness poll's worker_warning field", () => {
		const idx = chatSrc.indexOf("r && r.worker_warning");
		expect(idx, "the boot checkReady().then() block must read r.worker_warning").not.toBe(-1);
		expect(chatSrc.slice(idx, idx + 80)).toContain("WORKERS_WARN_MSG");
	});

	it("clears workersWarnNotice on the next successful retry, alongside workersNotice", () => {
		const fnStart = chatSrc.indexOf("async function retry(messageId)");
		const fnEnd = chatSrc.indexOf("\nasync function send(", fnStart);
		expect(fnStart, "ChatView must still define retry()").not.toBe(-1);
		expect(fnEnd, "ChatView must still define send() after retry()").not.toBe(-1);
		const body = chatSrc.slice(fnStart, fnEnd);
		const idx = body.indexOf("workersWarnNotice.value = null");
		expect(idx, "retry() must clear workersWarnNotice on a successful response").not.toBe(-1);
		// Same self-heal spot as workersNotice's own clear, immediately after it.
		const notice = body.indexOf("workersNotice.value = null");
		expect(notice).toBeGreaterThan(-1);
		expect(idx).toBeGreaterThan(notice);
		expect(idx - notice).toBeLessThan(80);
	});

	it("also clears workersWarnNotice on the next successful send (mirrors retry())", () => {
		const first = chatSrc.indexOf("workersWarnNotice.value = null");
		const second = chatSrc.indexOf("workersWarnNotice.value = null", first + 1);
		expect(
			second,
			"send() must also clear workersWarnNotice on a successful response"
		).not.toBe(-1);
		// Sits right after send()'s own workersNotice clear, same as retry() above.
		const notice = chatSrc.indexOf("workersNotice.value = null", first + 1);
		expect(notice).toBeGreaterThan(-1);
		expect(second).toBeGreaterThan(notice);
		expect(second - notice).toBeLessThan(80);
	});

	it("never gates canSend on workersWarnNotice (composer must stay enabled - heads-up only)", () => {
		const start = chatSrc.indexOf("const canSend = computed(");
		expect(start, "ChatView must still define canSend").not.toBe(-1);
		const end = chatSrc.indexOf("\n);", start);
		expect(chatSrc.slice(start, end)).not.toContain("workersWarnNotice");
	});
});

describe("replaced-site banner", () => {
	it("names the site the account moved to", () => {
		const b = replacedBanner({ replaced: true, moved_to: "https://jarvis-2.example.com/" });
		expect(b.message).toContain("jarvis-2.example.com");
		expect(b.message).not.toContain("https://");
		expect(b.action).toBeTruthy();
	});

	it("still explains itself without a destination", () => {
		expect(replacedBanner({ replaced: true }).message).toContain("another site");
	});

	it("stays silent when nothing moved", () => {
		expect(replacedBanner({})).toBeNull();
		expect(replacedBanner(null)).toBeNull();
		expect(replacedBanner({ replaced: false })).toBeNull();
	});
});

describe("reconnect intent", () => {
	it("recognises only the flag the banner actually sends", () => {
		expect(hasReconnectIntent(new URL(RECONNECT_INTENT_URL, "http://x").search)).toBe(true);
		expect(hasReconnectIntent("?reconnect=1&foo=bar")).toBe(true);
		expect(hasReconnectIntent("?reconnect=0")).toBe(false);
		expect(hasReconnectIntent("?other=1")).toBe(false);
	});

	it("treats missing or unparseable input as no intent", () => {
		expect(hasReconnectIntent("")).toBe(false);
		expect(hasReconnectIntent(null)).toBe(false);
		expect(hasReconnectIntent(undefined)).toBe(false);
	});
});

describe("landing step", () => {
	it("sends a reconnect intent to the step that carries the offer", () => {
		expect(landingStep({ intent: true, resumedStep: "pay", terminal: false })).toBe("details");
		expect(landingStep({ intent: true, resumedStep: "intro", terminal: false })).toBe(
			"details"
		);
	});

	it("leaves the resumed step alone without the intent", () => {
		expect(landingStep({ intent: false, resumedStep: "pay", terminal: false })).toBe("pay");
		expect(landingStep({ intent: false, resumedStep: "intro", terminal: false })).toBe(
			"intro"
		);
	});

	it("never drags a paid or provisioning signup into reconnect", () => {
		expect(landingStep({ intent: true, resumedStep: "connect", terminal: true })).toBe(
			"connect"
		);
	});
});
