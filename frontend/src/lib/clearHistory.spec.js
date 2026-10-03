import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { CLEAR_HISTORY_CONFIRM, clearHistoryOutcome, keptChatsNotice } from "./clearHistory";

// "Delete all chat history" keeps a chat that is waiting for a reply (the server
// answers { ok, deleted, skipped, kept: [names] }). What the user is told, and
// whether the chat they have open stays open, is decided here.

describe("keptChatsNotice", () => {
	it("says nothing when every chat was deleted", () => {
		expect(keptChatsNotice(0)).toBe("");
	});

	it("names one kept chat in the singular", () => {
		expect(keptChatsNotice(1)).toBe(
			"1 chat was kept because a reply is still in progress. Delete again once it finishes."
		);
	});

	it("names several kept chats in the plural", () => {
		expect(keptChatsNotice(2)).toBe(
			"2 chats were kept because a reply is still in progress. Delete again once they finish."
		);
	});
});

describe("clearHistoryOutcome", () => {
	it("is the old outcome when nothing was skipped", () => {
		const out = clearHistoryOutcome({ ok: true, deleted: 4, skipped: 0, kept: [] }, "c-open");
		expect(out).toEqual({ notice: "", openChatKept: false });
	});

	it("is the old outcome for a server that does not report skipped chats yet", () => {
		for (const res of [{ ok: true, deleted: 4 }, {}, null, undefined])
			expect(clearHistoryOutcome(res, "c-open")).toEqual({
				notice: "",
				openChatKept: false,
			});
	});

	it("keeps the open chat open when it is one of the kept ones", () => {
		const out = clearHistoryOutcome(
			{ ok: true, deleted: 3, skipped: 2, kept: ["c-other", "c-open"] },
			"c-open"
		);
		expect(out.openChatKept).toBe(true);
		expect(out.notice).toBe(keptChatsNotice(2));
	});

	it("still leaves the open chat when another chat was the kept one", () => {
		const out = clearHistoryOutcome(
			{ ok: true, deleted: 3, skipped: 1, kept: ["c-other"] },
			"c-open"
		);
		expect(out).toEqual({ notice: keptChatsNotice(1), openChatKept: false });
	});

	it("does not treat an unsaved new chat as kept", () => {
		for (const open of ["", null, undefined])
			expect(
				clearHistoryOutcome({ ok: true, deleted: 0, skipped: 1, kept: [""] }, open)
					.openChatKept
			).toBe(false);
	});

	it("counts the names when the count itself is missing or wrong", () => {
		expect(clearHistoryOutcome({ ok: true, kept: ["a", "b"] }, "x").notice).toBe(
			keptChatsNotice(2)
		);
		expect(clearHistoryOutcome({ ok: true, skipped: "3" }, "x").notice).toBe(
			keptChatsNotice(3)
		);
		expect(clearHistoryOutcome({ ok: true, skipped: -1 }, "x").notice).toBe("");
	});
});

describe("the confirmation", () => {
	it("says that a chat with a reply in progress is kept", () => {
		expect(CLEAR_HISTORY_CONFIRM).toContain("permanently deleted");
		expect(CLEAR_HISTORY_CONFIRM).toContain("A chat with a reply still in progress is kept.");
	});
});

describe("ChatView clearAllHistory", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	const start = src.indexOf("async function clearAllHistory(");
	const body = src.slice(start, src.indexOf("\n}\n", start));

	it("decides from the server's answer", () => {
		expect(start).toBeGreaterThan(-1);
		expect(body).toMatch(
			/const outcome = clearHistoryOutcome\(await api\.clearChatHistory\(\), currentId\.value\);/
		);
		expect(body).toContain("message: CLEAR_HISTORY_CONFIRM,");
	});

	it("tells the user about the kept chats", () => {
		expect(body).toMatch(/if \(outcome\.notice\) notify\(outcome\.notice, \{/);
	});

	it("returns before it blanks the view when the open chat was kept", () => {
		const keep = body.indexOf("if (outcome.openChatKept) {");
		const blank = body.indexOf("messages.value = [];");
		expect(keep).toBeGreaterThan(-1);
		expect(blank).toBeGreaterThan(keep);
		const kept = body.slice(keep, blank);
		expect(kept).toContain("return;");
		// the kept chat is left exactly as it is: nothing here resets it
		expect(kept).not.toMatch(/currentId\.value =|newChat\(|resetRunState\(/);
		// the sidebar still drops the chats that did go
		expect(kept).toContain("store.loadConversations();");
	});

	it("resets to a new chat exactly as before otherwise", () => {
		expect(body).toMatch(
			/messages\.value = \[\];\s*originPage\.value = "";\s*originOf\.value = "";\s*currentId\.value = "";\s*settingsOpen\.value = false;\s*newChat\(\);/
		);
	});
});
