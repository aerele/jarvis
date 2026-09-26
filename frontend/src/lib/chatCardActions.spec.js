import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	chatOutcomeMessage,
	chatRefusalMessage,
	chatSettledReason,
	chatStatusLine,
	isChatSettled,
	keepsChatCard,
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
			// approve_and_run (C5): the card stays Pending on every one of these -
			// see the "keeps a card" case below.
			"not_runnable",
			"needs_own_confirm",
			"skill_not_armed",
			"storage_unavailable",
			"storage_outcome_unknown",
		]) {
			const words = chatRefusalMessage({ ok: false, reason_code: code });
			expect(words, code).toMatch(/\w/);
			expect(words, code).not.toContain("undefined");
			expect(words, code).not.toBe("This action could not be completed.");
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
});

describe("keepsChatCard", () => {
	it("keeps a card unless the answer settled it", () => {
		for (const code of [
			"busy",
			"identity_refused",
			"armed_run",
			// approve_and_run (C5): the server keeps the response shape + these new
			// codes, and the card stays Pending on every one of them.
			"not_runnable",
			"needs_own_confirm",
			"skill_not_armed",
			"storage_unavailable",
			"storage_outcome_unknown",
		])
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

// D2: cards never expire now, so confirming one whose target changed settles
// it as Failed with a specific reason_code (stale/target_missing/tampered/
// unverifiable) alongside error.type InvalidConfirmation. keepsChatCard
// already treats that as settled (isChatSettled's pa_status "Failed" check),
// so the generic InvalidConfirmation branch below must show the SPECIFIC
// reason instead of guessing "another tab" - that guess is for a truly bare
// legacy token (no reason_code at all).
describe("chatSettledReason", () => {
	it("gives the specific reason for a card that settled as Failed with a known reason_code", () => {
		expect(
			chatSettledReason({
				ok: false,
				reason_code: "stale",
				pa_status: "Failed",
				error: { type: "InvalidConfirmation" },
			})
		).toContain("record changed");
		expect(
			chatSettledReason({ ok: false, reason_code: "target_missing", pa_status: "Failed" })
		).toContain("no longer exists");
		expect(
			chatSettledReason({ ok: false, reason_code: "tampered", pa_status: "Failed" })
		).toContain("integrity check");
		expect(
			chatSettledReason({ ok: false, reason_code: "unverifiable", pa_status: "Failed" })
		).toContain("verified");
	});
	it("gives nothing for a bare legacy token (no reason_code at all)", () => {
		expect(chatSettledReason({ ok: false, error: { type: "InvalidConfirmation" } })).toBe("");
	});
	it("gives nothing for a successful or missing response", () => {
		expect(chatSettledReason({ ok: true })).toBe("");
		expect(chatSettledReason(null)).toBe("");
	});
});

describe("ChatView shows the specific reason on a settled card", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	for (const fn of ["confirmPending", "approveAndRunPending", "onTypedConfirmResolved"]) {
		it(`${fn} checks chatSettledReason in its InvalidConfirmation branch`, () => {
			const start = src.indexOf(`async function ${fn}(`);
			const body = src.slice(start, src.indexOf("\nasync function ", start + 1));
			const invalid = body.indexOf('r.error.type === "InvalidConfirmation"');
			const reason = body.indexOf("chatSettledReason(r)");
			expect(invalid).toBeGreaterThan(-1);
			expect(reason).toBeGreaterThan(invalid);
		});
	}
});
