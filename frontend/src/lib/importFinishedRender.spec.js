import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * Slice B ("auto-tell import completion"): when a background Data Import
 * finishes, the bench posts a message into the originating conversation and
 * publishes `{ kind: "import:finished", conversation_id, message_id }` to the
 * conversation owner's socket. Two disjoint listeners share this socket:
 *
 *   - ChatView's onEvent (this file) owns the ON-SCREEN case: the
 *     conversation-id guard already filtered to the conversation the user is
 *     looking at, so the case just re-reads the transcript, the same way
 *     message:enriched does.
 *   - globalNotifier.js (see its own spec) owns the OFF-SCREEN case: the
 *     sidebar unread dot, with no toast.
 *
 * ChatView.vue can't be mounted here (router/socket/api surface), so — like
 * pendingConfirmResync.spec.js — the wiring is asserted against the source.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

function caseBody(kind) {
	const at = src.indexOf(`case "${kind}":`);
	expect(at).toBeGreaterThan(-1);
	// Up to the next `case "` at the same switch level is close enough to scope it.
	const rest = src.slice(at + `case "${kind}":`.length);
	const end = rest.indexOf('\t\tcase "');
	return end === -1 ? rest : rest.slice(0, end);
}

describe("ChatView renders a finished import live when its conversation is open", () => {
	it("onEvent's switch has a dedicated import:finished case", () => {
		expect(src).toContain('case "import:finished":');
	});

	it("the case re-reads the transcript the same way message:enriched does", () => {
		const body = caseBody("import:finished");
		expect(body).toContain("loadConversation(currentId.value);");
	});

	it("the case sits behind the conversation_id === currentId guard, like message:enriched", () => {
		// Both cases live inside the same switch, which onEvent only reaches
		// after `if (p.conversation_id !== currentId.value) return;` — pin the
		// ordering so a future refactor can't hoist import:finished above it
		// (the way action:pending/macro:*/conversation:new are, deliberately,
		// for THEIR off-screen needs).
		const guardAt = src.indexOf("if (p.conversation_id !== currentId.value) return;");
		const switchAt = src.indexOf("switch (p.kind) {");
		const caseAt = src.indexOf('case "import:finished":');
		expect(guardAt).toBeGreaterThan(-1);
		expect(guardAt).toBeLessThan(switchAt);
		expect(switchAt).toBeLessThan(caseAt);
	});

	it("does not run the pump-epoch fence — this is a scheduler-originated signal, not a turn frame", () => {
		const body = caseBody("import:finished");
		expect(body).not.toContain("pumpFenceReject");
		expect(body).not.toContain("pumpFenceAccept");
	});
});
