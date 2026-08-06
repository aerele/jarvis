import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * A parked confirmation card must never depend on a single event surviving.
 *
 * It can reach the screen by three best-effort routes, and each one can miss:
 *
 *   1. the live `action:pending` push. Socketio has no replay, so anything
 *      published during a blip is simply gone.
 *   2. the `pending` payload folded into a terminal by _extra_with_pending.
 *      Only settlement/finalize do that; turn_handler's own run:end has no
 *      `pending` key.
 *   3. the loadConversation reload (which resyncs as a side effect). On the
 *      ordinary success path run:end deliberately SKIPS it to avoid a visible
 *      flash, leaving the card waiting on message:enriched, another event.
 *
 * Three independent maybes multiply into the rare miss users describe as "about
 * one confirmation in ten never showed". The fix is an unconditional resync from
 * durable server state at every terminal. These tests pin it there.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

const terminalBody = (kind) => {
	const at = src.indexOf(`case "${kind}":`);
	expect(at).toBeGreaterThan(-1);
	// Up to the next `case "` at the same switch level is close enough to scope it.
	const rest = src.slice(at + 10);
	const end = rest.indexOf('\t\tcase "');
	return end === -1 ? rest : rest.slice(0, end);
};

describe("parked confirmations resync from durable state at every terminal", () => {
	it("run:end resyncs unconditionally, not only when it carried a pending payload", () => {
		const body = terminalBody("run:end");
		expect(body).toContain("resyncPendingConfirmations(currentId.value);");
		// It must NOT sit inside the `if (Array.isArray(p.pending))` branch: that is
		// precisely the payload that can be absent.
		const guard = body.indexOf("if (Array.isArray(p.pending))");
		const call = body.indexOf("resyncPendingConfirmations(currentId.value);");
		const guardCloses = body.indexOf("\t\t\t}", guard);
		expect(call).toBeGreaterThan(guardCloses);
	});

	it("run:error resyncs too, so a card parked in a turn that then errors recovers", () => {
		const body = terminalBody("run:error");
		expect(body).toContain("resyncPendingConfirmations(currentId.value);");
	});

	it("the resync is token-deduped, so a live push plus a resync cannot double-render", () => {
		expect(src).toContain("function enqueuePending(card) {");
		const fn = src.slice(src.indexOf("function enqueuePending(card) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("pendingActions.value.some((x) => x.token === card.token)");
	});

	it("the resync is freshness-guarded against a mid-flight conversation switch", () => {
		const fn = src.slice(src.indexOf("async function resyncPendingConfirmations(id) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("if (currentId.value !== id) return;");
		// and never throws into the terminal handler that now calls it unawaited
		expect(body).toContain("catch (e) {");
	});

	it("loadConversation still resyncs, so a reload/reconnect path keeps working", () => {
		expect(src).toContain("resyncPendingConfirmations(id);");
	});
});
