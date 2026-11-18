// The shared decision (lib/chatCardActions.js: confirmReplyOutcome, dismissReplyOutcome,
// thrownCardError) over the table of real server replies in chatCardReplyCases.js. The desktop and
// the phone apply what it returns, so what it says for a reply is what both do.
//
// The expectations (EXPECTED) are written by hand, not generated: chatCardDoors.spec.js already
// holds that the desktop does what its old handlers did; this holds what the answer MUST be, so a
// regenerated golden cannot bless a wrong one, and the phone's host can be held to the same rows.
import { describe, expect, it } from "vitest";
import { confirmReplyOutcome, dismissReplyOutcome, thrownCardError } from "./chatCardActions.js";
import { EXPECTED, REPLY_ROWS, THROWN_FALLBACK, thrownError } from "./chatCardReplyCases.js";

// "$error" / "$message" stand for the card error the outcome carries (see EXPECTED).
const want = (row, expected) => {
	const { draft, cardError, ...rest } = expected;
	return {
		...rest,
		cardError:
			cardError === "$error"
				? row.reply.error
				: cardError === "$message"
				? { message: expected.message }
				: null,
	};
};

describe("every real reply has a decision", () => {
	// Break: a row added to the table without saying what it must do.
	it("EXPECTED has exactly the rows of the table", () => {
		expect(Object.keys(EXPECTED).sort()).toEqual(REPLY_ROWS.map((r) => r.id).sort());
	});
});

describe("Create / Confirm / Approve & run", () => {
	// both buttons answer in the same envelopes; the approve-only rows (the storage reply that carries
	// a reason_code, the refusals only approve_and_run sends) are asserted in full too
	const rows = REPLY_ROWS.filter(
		(r) => (r.doors.includes("confirm") || r.doors.includes("approve")) && !r.thrown
	);
	// Break: any change in whether a reply keeps the card, what it says, whether the desktop
	// toasts or reloads, the queued chip, the safety notice, or the card error object.
	for (const row of rows) {
		it(`${row.id}`, () => {
			const expected = EXPECTED[row.id];
			const out = confirmReplyOutcome(row.reply, { expired: row.expires === "past" });
			expect(out).toEqual(want(row, expected));
			// a server draft card differs only where the table says so
			const draftOut = confirmReplyOutcome(row.reply, {
				draft: true,
				expired: row.expires === "past",
			});
			expect(draftOut).toEqual(want(row, expected.draft || expected));
		});
	}
});

describe("Cancel / Discard", () => {
	// Break: as above, for dismiss_tool (which has no InvalidConfirmation branch: a refusal with
	// no reason_code means the card is spent, and the safety code is read whatever `ok` says).
	for (const row of REPLY_ROWS.filter((r) => r.doors.includes("dismiss") && !r.thrown)) {
		it(`${row.id}`, () => {
			expect(dismissReplyOutcome(row.reply)).toEqual(want(row, EXPECTED[row.id]));
		});
	}
});

describe("a call that throws", () => {
	// Break: the sanitised text of a failed call shows the raw error text again, or the door's
	// fallback changes. A message the server wrote on purpose, an expired session and an offline
	// phone are plain messages; anything else is internal and says the fallback.
	for (const row of REPLY_ROWS.filter((r) => r.thrown)) {
		for (const door of ["confirm", "approve", "dismiss"]) {
			if (
				!(door === "dismiss"
					? row.doors.includes("dismiss")
					: row.doors.includes("confirm"))
			)
				continue;
			it(`${row.id} (${door})`, () => {
				const got = thrownCardError(thrownError(row.thrown), door);
				const expected = EXPECTED[row.id].thrown;
				if (expected.internal) {
					expect(got).toMatchObject({
						code: "InternalError",
						message: THROWN_FALLBACK[door],
					});
				} else {
					expect(got).toEqual({ message: expected.message });
				}
				// never the raw text of the error
				expect(got.message).not.toBe(row.thrown.message);
			});
		}
	}
});
