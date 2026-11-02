import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	CLEAR_HISTORY_CONFIRM,
	clearHistoryFailedNotice,
	clearHistoryOutcome,
	failedChatsNotice,
	keptChatsNotice,
	retainedCountPlanNotice,
} from "./clearHistory";

// "Delete all chat history" keeps a chat for a live reply or retained count
// evidence, and may fail on a chat. What the user is told, and whether the chat
// they have open stays open, is decided here.

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
		for (const text of [
			ONE,
			TWO,
			keptChatsNotice(2, 3),
			retainedCountPlanNotice(2),
			clearHistoryFailedNotice(""),
			failedChatsNotice(1),
			failedChatsNotice(2),
		])
			expect(text).not.toContain("\u2014");
	});
});

describe("failedChatsNotice", () => {
	it("says nothing when no chat failed", () => {
		for (const failed of [0, undefined, NaN, -1]) expect(failedChatsNotice(failed)).toBe("");
	});

	it("says plainly that the chats could not be deleted, and to try again", () => {
		expect(failedChatsNotice(1)).toBe("1 chat could not be deleted. Try Delete all again.");
		expect(failedChatsNotice(4)).toBe("4 chats could not be deleted. Try Delete all again.");
	});

	it("does not claim a reply is in progress", () => {
		for (const failed of [1, 4]) expect(failedChatsNotice(failed)).not.toMatch(/repl/i);
	});
});

describe("retainedCountPlanNotice", () => {
	it("explains retained physical-count evidence without suggesting a retry", () => {
		const one = retainedCountPlanNotice(1);
		const many = retainedCountPlanNotice(2);
		expect(one).toBe(
			"1 chat was kept because it contains physical count evidence. Open Physical count plans to view it."
		);
		expect(many).toBe(
			"2 chats were kept because they contain physical count evidence. Open Physical count plans to view them."
		);
		expect(one).not.toMatch(/reply|again/i);
		expect(retainedCountPlanNotice(0)).toBe("");
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
		expect(out).toEqual({ notice: "", failed: false, openChatKept: false });
	});

	it("is the old outcome for a server that does not report skipped chats yet", () => {
		for (const res of [{ ok: true, deleted: 4 }, {}, null, undefined])
			expect(clearHistoryOutcome(res, "c-open")).toEqual({
				notice: "",
				failed: false,
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
		expect(out).toEqual({
			notice: keptChatsNotice(1, 3),
			failed: false,
			openChatKept: false,
		});
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

	it("explains retained plans instead of a reply that cannot finish", () => {
		const out = clearHistoryOutcome(
			{
				ok: true,
				deleted: 2,
				skipped: 1,
				kept: ["count-plan-chat"],
				retained_count_plan_names: ["count-plan-chat"],
			},
			"count-plan-chat"
		);
		expect(out).toEqual({
			notice: "Deleted 2 chats. 1 chat was kept because it contains physical count evidence. Open Physical count plans to view it.",
			failed: false,
			openChatKept: true,
		});
	});

	it("states each retained reason when live replies and retained plans mix", () => {
		const out = clearHistoryOutcome(
			{
				ok: true,
				deleted: 1,
				skipped: 2,
				kept: ["live-chat", "count-plan-chat"],
				retained_count_plan_names: ["count-plan-chat"],
			},
			"count-plan-chat"
		);
		expect(out.notice).toBe(
			"Deleted 1 chat. 1 chat was kept because a reply is still in progress. Delete all again once the reply finishes. 1 chat was kept because it contains physical count evidence. Open Physical count plans to view it."
		);
		expect(out.openChatKept).toBe(true);
	});

	it("ignores retained names that the server did not keep", () => {
		expect(
			clearHistoryOutcome(
				{
					ok: true,
					deleted: 0,
					skipped: 1,
					kept: ["live-chat"],
					retained_count_plan_names: ["other"],
				},
				"live-chat"
			).notice
		).toBe(keptChatsNotice(1, 0));
	});
});

describe("clearHistoryOutcome when chats could not be deleted", () => {
	const res = (over) => ({
		ok: true,
		deleted: 3,
		skipped: 0,
		kept: [],
		failed: 0,
		failed_names: [],
		...over,
	});

	it("says how many went and how many could not be deleted", () => {
		const out = clearHistoryOutcome(res({ failed: 2, failed_names: ["a", "b"] }), "c-open");
		expect(out).toEqual({
			notice: "Deleted 3 chats. 2 chats could not be deleted. Try Delete all again.",
			failed: true,
			openChatKept: false,
		});
	});

	it("does not tell the user a reply is in progress for them", () => {
		const out = clearHistoryOutcome(res({ deleted: 0, failed: 1, failed_names: ["a"] }), "");
		expect(out.notice).toBe(
			"No chats were deleted. 1 chat could not be deleted. Try Delete all again."
		);
		expect(out.notice).not.toMatch(/repl/i);
	});

	it("keeps the open chat open when it is one that could not be deleted", () => {
		const out = clearHistoryOutcome(
			res({ failed: 2, failed_names: ["c-other", "c-open"] }),
			"c-open"
		);
		expect(out.openChatKept).toBe(true);
		expect(out.failed).toBe(true);
	});

	it("says both when some were kept for a reply and some failed", () => {
		const out = clearHistoryOutcome(
			res({ skipped: 1, kept: ["c-busy"], failed: 2, failed_names: ["a", "b"] }),
			"c-busy"
		);
		expect(out).toEqual({
			notice: `${keptChatsNotice(1, 3)} ${failedChatsNotice(2)}`,
			failed: true,
			openChatKept: true,
		});
	});

	it("counts the names when the count itself is missing or wrong", () => {
		expect(clearHistoryOutcome({ ok: true, failed_names: ["a", "b"] }, "").notice).toBe(
			failedChatsNotice(2)
		);
		expect(clearHistoryOutcome({ ok: true, failed: "3" }, "").notice).toBe(
			failedChatsNotice(3)
		);
		expect(clearHistoryOutcome({ ok: true, failed: -1 }, "")).toEqual({
			notice: "",
			failed: false,
			openChatKept: false,
		});
	});

	it("reads a server that reports no failures as none", () => {
		const out = clearHistoryOutcome({ ok: true, deleted: 3, skipped: 1, kept: ["a"] }, "a");
		expect(out).toEqual({ notice: keptChatsNotice(1, 3), failed: false, openChatKept: true });
	});
});

describe("the confirmation", () => {
	it("says that live replies and physical-count evidence are kept", () => {
		expect(CLEAR_HISTORY_CONFIRM).toContain("permanently deleted");
		expect(CLEAR_HISTORY_CONFIRM).toContain(
			"reply in progress or physical count evidence are kept"
		);
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
			"Every conversation and message, permanently. Chats with a reply in progress or physical count evidence are kept. Macros and skills stay."
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

	it("tells the user about the chats that stayed, as an error when any failed", () => {
		expect(body).toMatch(/if \(outcome\.notice\)\s+notify\(outcome\.notice, \{/);
		expect(body).toContain('type: outcome.failed ? "error" : "info",');
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
