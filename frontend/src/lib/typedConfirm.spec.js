import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * A typed go-ahead is answered by the server running the confirmation, NOT by
 * starting a turn. So no run:start, no assistant:delta and no run:end are
 * coming, and the client has to take the spinner down itself. If it does not,
 * the composer locks forever on the one interaction the whole gate depends on.
 *
 * These pin the client half of that contract at the source level, the same way
 * chatAction.spec.js pins the two gates that used to disagree.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

// send()'s confirmed handling sits on the ACCEPTED path, after the rejection
// block. It has to: the voice-dictation lifecycle tests anchor on the FIRST
// occurrence of the one-shot context clear and the voice release, so a second
// copy of either above the rejection block silently moves their anchor.
const CONFIRMED = "if (r && r.confirmed) {";
const confirmedAt = src.indexOf(CONFIRMED);
const sendConfirmedBlock = src.slice(confirmedAt, confirmedAt + 700);

describe("send() handles a confirmed response", () => {
	it("keeps a confirmed response OUT of the rejection branch", () => {
		// A failed confirmation carries ok:false AND confirmed:true. Falling into the
		// rejection branch would restore the text to the composer and tell the user
		// the send failed, when in fact their card was spent. The guard excludes it
		// explicitly rather than relying on branch order.
		expect(confirmedAt).toBeGreaterThan(-1);
		expect(src).toContain("if (r && r.ok === false && !r.confirmed) {");
	});

	it("reuses the accepted path's one-shot clear and voice release", () => {
		// Duplicating them above the rejection block is what broke the voice
		// lifecycle tests, which anchor on the FIRST occurrence of each.
		expect(src.indexOf("_prefillSendContext = null;")).toBeLessThan(confirmedAt);
		expect(src.indexOf("if (_voiceAck) voiceStore?.acknowledge(_voiceAck);")).toBeLessThan(
			confirmedAt
		);
	});

	it("unlocks the composer, since no run will arrive to do it", () => {
		const block = sendConfirmedBlock;
		expect(block).toContain("sending.value = false;");
		expect(block).toContain("waiting.value = false;");
	});

	it("retires EVERY card the server confirmed, not just the first", () => {
		// A bulk approval spends N tokens in one response. Removing one would leave
		// the rest on screen as live-looking offers for writes that already ran.
		expect(sendConfirmedBlock).toContain("for (const t of r.tokens || []) removePending(t);");
	});

	it("drops the optimistic bubble, because nothing was persisted for it", () => {
		// The button records no user message either. Leaving the bubble would show a
		// message that a reload does not bring back.
		const block = sendConfirmedBlock;
		expect(block).toContain("messages.value.filter((x) => x.name !== tmpName)");
	});

	it("routes both outcomes through one shared resolver", () => {
		// The typed path and the button must not drift in what the user then sees.
		expect(src).toContain("async function onTypedConfirmResolved(r)");
		expect(src).toContain("await onTypedConfirmResolved(r);");
	});
});

describe("the shared resolver covers every outcome", () => {
	const fnAt = src.indexOf("async function onTypedConfirmResolved(r)");
	const fn = src.slice(fnAt, src.indexOf("// Dismiss: consume the token server-side", fnAt));

	it("surfaces a storage outage and re-reads the parked list", () => {
		// The write did NOT run and the card is already off screen, so the list has
		// to be re-read or the user loses the card entirely.
		expect(fn).toContain("confirmationStorageUnavailable(r)");
		expect(fn).toContain("resyncPendingConfirmations(currentId.value)");
	});

	it("explains an invalid token instead of failing silently", () => {
		expect(fn).toContain('r.error.type === "InvalidConfirmation"');
	});

	it("reloads so the durable receipt chip is what the user sees", () => {
		expect(fn).toContain("loadConversation(currentId.value)");
	});

	it("raises the queued chip when the continuation is queued", () => {
		expect(fn).toContain("queuedTurn.value = {");
		expect(fn).toContain("sending.value = true;");
	});
});

describe("the card advertises both ways to approve", () => {
	it("shows the hint once per stack, on the last card", () => {
		// A queue of five must not repeat the same line five times.
		expect(src).toContain('v-if="pi === visiblePendingActions.length - 1"');
	});

	it("teaches bulk and selective forms only when there is a choice to make", () => {
		expect(src).toContain('or type "confirm all", or "confirm 1 and 3"');
		expect(src).toContain('or type "go ahead"');
	});

	it("numbers the cards, since a typed selection picks by that number", () => {
		expect(src).toContain('v-if="visiblePendingActions.length > 1" class="jv-pending-num"');
		expect(src).toContain("{{ pi + 1 }} of {{ visiblePendingActions.length }}");
	});

	it("orders the cards the way the server orders them", () => {
		// If the screen and the server disagree about which card is number 1, a
		// typed "confirm 1" runs the wrong write.
		expect(src).toContain("(a.expires_at || 0) - (b.expires_at || 0) ||");
	});
});
