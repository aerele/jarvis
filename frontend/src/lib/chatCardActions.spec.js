import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	chatOutcomeMessage,
	chatRefusalMessage,
	chatStatusLine,
	isChatSettled,
	keepsChatCard,
	laneHeading,
} from "./chatCardActions";

describe("chatCardActions copy", () => {
	it("words every refusal code the chat endpoints answer with", () => {
		for (const code of [
			"not_found",
			"already_handled",
			"busy",
			"executing",
			"armed_run",
			"identity_refused",
			"stale",
			"target_missing",
			"tampered",
			"unverifiable",
		]) {
			const words = chatRefusalMessage({ ok: false, reason_code: code });
			expect(words, code).toMatch(/\w/);
			expect(words, code).not.toContain("undefined");
		}
		expect(chatRefusalMessage({ reason_code: "stale" })).toContain("Nothing ran");
	});

	it("keeps the tool's own words for a failed run, and the server's for the rest", () => {
		const failed = {
			ok: false,
			reason_code: "failed",
			error: { message: "Due Date is required" },
		};
		expect(chatRefusalMessage(failed)).toBe("Couldn't complete it: Due Date is required");
		const partial = { ok: false, reason_code: "partial", error: { message: "Row 3 failed." } };
		expect(chatRefusalMessage(partial)).toContain("partly run");
		const storage = {
			ok: false,
			error: { type: "ConfirmationUnavailableError", message: "Try again." },
		};
		expect(chatRefusalMessage(storage)).toBe("Try again.");
		expect(chatRefusalMessage({})).toBe("This action could not be completed.");
	});

	it("says what happened after a decision", () => {
		expect(chatOutcomeMessage({ ok: true }, "confirm")).toBe(
			"Confirmed. Jarvis continues in the chat."
		);
		expect(chatOutcomeMessage({ ok: true, queued: true }, "confirm")).toContain(
			"once it's free"
		);
		expect(chatOutcomeMessage({ ok: true, reason_code: "discarded" }, "discard")).toBe(
			"Discarded. Nothing ran."
		);
		expect(chatOutcomeMessage({ ok: true, reason_code: "already_handled" }, "discard")).toBe(
			"This action was already handled."
		);
	});

	it("drops a card that is decided or gone, and keeps one that is only busy", () => {
		expect(isChatSettled({ ok: true })).toBe(true);
		expect(isChatSettled({ ok: false, reason_code: "failed", pa_status: "Failed" })).toBe(
			true
		);
		expect(isChatSettled({ ok: false, reason_code: "not_found" })).toBe(true);
		expect(isChatSettled({ ok: false, error: { type: "InvalidConfirmation" } })).toBe(true);
		expect(
			isChatSettled({
				ok: false,
				reason_code: "busy",
				error: { type: "InvalidConfirmation" },
			})
		).toBe(false);
		expect(isChatSettled({ ok: false, reason_code: "executing" })).toBe(false);
		expect(isChatSettled(null)).toBe(false);
	});

	it("gives a closed card a status line", () => {
		expect(chatStatusLine({ status: "Executing" })).toBe("Running now…");
		expect(chatStatusLine({ status: "Executed" })).toBe("Confirmed.");
		expect(chatStatusLine({ status: "Discarded" })).toBe("Discarded. Nothing ran.");
		expect(
			chatStatusLine({
				status: "Superseded",
				reason: "A newer proposal replaced this action.",
			})
		).toBe("A newer proposal replaced this action.");
	});

	it("heads the lane by where its rows come from", () => {
		expect(laneHeading([{ kind: "file_box_held" }]).title).toBe(
			"New records waiting for approval"
		);
		expect(laneHeading([{ kind: "chat" }])).toEqual({
			title: "Actions waiting for your confirmation",
			source: "from your chats",
		});
		expect(laneHeading([{ kind: "chat" }, { kind: "file_box_held" }]).source).toBe(
			"from File Box runs and your chats"
		);
	});
});

describe("keepsChatCard", () => {
	it("keeps a card unless the answer settled it", () => {
		for (const code of ["busy", "identity_refused", "armed_run"])
			expect(keepsChatCard({ ok: false, reason_code: code }), code).toBe(true);
		const running = { ok: false, reason_code: "executing", pa_status: "Executing" };
		expect(keepsChatCard(running)).toBe(true);
		expect(chatRefusalMessage(running)).toBe("This action is already running.");
		for (const res of [
			{ ok: true },
			{ ok: false, reason_code: "already_handled" },
			{ ok: false, reason_code: "not_found" },
			{ ok: false, reason_code: "failed", pa_status: "Failed" },
			{ ok: false, reason_code: "stale", pa_status: "Failed" },
			{ ok: false, error: { type: "InvalidConfirmation" } },
			{ ok: false, error: { type: "ValidationError", message: "legacy tool failed" } },
			null,
		])
			expect(keepsChatCard(res)).toBe(false);
	});
});

describe("ChatView keeps a refused card", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	for (const fn of ["confirmPending", "approveAndRunPending", "discardPending"]) {
		it(`${fn} checks keepsChatCard before it removes the card`, () => {
			const start = src.indexOf(`async function ${fn}(`);
			const body = src.slice(start, src.indexOf("\nasync function ", start + 1));
			const kept = body.indexOf("keepsChatCard(r)");
			expect(kept).toBeGreaterThan(-1);
			expect(kept).toBeLessThan(body.indexOf("removePending(token)"));
		});
	}
});
