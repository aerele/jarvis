import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	CLEAR_HISTORY_CONFIRM,
	clearHistoryFailedNotice,
	clearHistoryOutcome,
	keptChatsNotice,
} from "./clearHistory";

// "Delete all chat history" keeps a chat that is waiting for a reply (the server
// answers { ok, deleted, skipped, kept: [names] }). What the user is told, and
// whether the chat they have open stays open, is decided here.

describe("keptChatsNotice", () => {
	const ONE =
		"1 chat was kept because a reply is still in progress. Delete all again once the reply finishes.";
	const TWO =
		"2 chats were kept because their replies are still in progress. Delete all again once the replies finish.";

	it("says nothing when every chat was deleted", () => {
		expect(keptChatsNotice(0, 4)).toBe("");
		expect(keptChatsNotice(0)).toBe("");
	});

	it("names one kept chat in the singular", () => {
		expect(keptChatsNotice(1)).toBe(ONE);
	});

	it("names several kept chats in the plural", () => {
		expect(keptChatsNotice(2)).toBe(TWO);
	});

	it("says how many were deleted, in the singular and the plural", () => {
		expect(keptChatsNotice(1, 1)).toBe(`Deleted 1 chat. ${ONE}`);
		expect(keptChatsNotice(2, 3)).toBe(`Deleted 3 chats. ${TWO}`);
		expect(keptChatsNotice(1, 0)).toBe(`No chats were deleted. ${ONE}`);
	});

	it("leaves the deleted count out when it is not a count", () => {
		for (const deleted of [undefined, NaN, -1]) expect(keptChatsNotice(1, deleted)).toBe(ONE);
	});

	it("has no em-dash", () => {
		for (const text of [ONE, TWO, keptChatsNotice(2, 3), clearHistoryFailedNotice("")])
			expect(text).not.toContain("\u2014");
	});
});

describe("clearHistoryFailedNotice", () => {
	it("says some chats may already be gone", () => {
		expect(clearHistoryFailedNotice("")).toBe(
			"Could not delete history. Some chats may already have been deleted."
		);
		expect(clearHistoryFailedNotice(undefined)).toBe(clearHistoryFailedNotice(""));
	});

	it("keeps the server's own message in front", () => {
		expect(clearHistoryFailedNotice("Request timed out.")).toBe(
			"Request timed out. Some chats may already have been deleted."
		);
		expect(clearHistoryFailedNotice("Server error")).toBe(
			"Server error. Some chats may already have been deleted."
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
		expect(out.notice).toBe(keptChatsNotice(2, 3));
	});

	it("still leaves the open chat when another chat was the kept one", () => {
		const out = clearHistoryOutcome(
			{ ok: true, deleted: 3, skipped: 1, kept: ["c-other"] },
			"c-open"
		);
		expect(out).toEqual({ notice: keptChatsNotice(1, 3), openChatKept: false });
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

	it("says how many were deleted beside how many were kept", () => {
		expect(
			clearHistoryOutcome({ ok: true, deleted: 0, skipped: 1, kept: ["a"] }, "").notice
		).toBe(keptChatsNotice(1, 0));
		expect(clearHistoryOutcome({ ok: true, deleted: "5", skipped: 2 }, "").notice).toBe(
			keptChatsNotice(2, 5)
		);
	});
});

describe("the confirmation", () => {
	it("says that a chat with a reply in progress is kept", () => {
		expect(CLEAR_HISTORY_CONFIRM).toContain("permanently deleted");
		expect(CLEAR_HISTORY_CONFIRM).toContain("A chat with a reply still in progress is kept.");
	});
});

describe("the Settings pane line", () => {
	it("names the exception too", () => {
		const pane = fs
			.readFileSync(
				path.resolve(__dirname, "../components/settings/GeneralPane.vue"),
				"utf8"
			)
			.replace(/\s+/g, " ");
		expect(pane).toContain(
			"Every conversation and message, permanently. Chats still waiting for a reply are kept. Macros and skills stay."
		);
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

	it("on a failure says some chats may be gone and reloads the list", () => {
		const failed = body.slice(body.indexOf("} catch (e) {"), body.indexOf("} finally {"));
		expect(failed).toContain(
			'notify(clearHistoryFailedNotice(errMessage(e)), { type: "error" });'
		);
		expect(failed).toContain("store.loadConversations();");
	});

	it("resets to a new chat exactly as before otherwise", () => {
		expect(body).toMatch(
			/messages\.value = \[\];\s*originPage\.value = "";\s*originOf\.value = "";\s*currentId\.value = "";\s*settingsOpen\.value = false;\s*newChat\(\);/
		);
	});
});
