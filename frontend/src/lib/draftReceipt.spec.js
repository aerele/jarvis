// The receipt of a decided server draft card (lib/draftReceipt.js), as the State Table words it.
import { describe, expect, it } from "vitest";
import { draftReceipt, isDraftReceiptRow } from "./draftReceipt.js";

const row = (over = {}, result = null, args = { doctype: "Timesheet" }) => ({
	role: "tool",
	tool_name: "create_doc",
	tool_call_id: "tok-1",
	tool_status: "",
	pending_card: null,
	action_outcome: "confirmed",
	tool_args: JSON.stringify(args),
	tool_result: result === null ? null : JSON.stringify(result),
	...over,
});
const ok = (data) => ({ ok: true, data: { doctype: "Timesheet", name: "TS-0001", ...data } });
const fail = (error, extra = {}) => ({ ok: false, error, ...extra });

describe("draftReceipt: the words of S6, S7, S8 and S10", () => {
	// Break: a receipt says something else than the State Table (the wording is the contract with the
	// phone and the person), loses its link or its Submit, or a record changed or deleted after the
	// draft reads as a plain failure.
	const table = [
		[
			"S6 created",
			row({}, ok()),
			{
				text: "Created Timesheet TS-0001",
				tone: "ok",
				open: { doctype: "Timesheet", name: "TS-0001" },
			},
		],
		[
			"S6 created, submittable and still a draft: Submit",
			row({}, ok({ suggested_next: { kind: "submit" } })),
			{
				text: "Created Timesheet TS-0001",
				tone: "ok",
				open: { doctype: "Timesheet", name: "TS-0001" },
				submit: true,
			},
		],
		[
			"S6 created, a workflow hint is not Submit",
			row({}, ok({ suggested_next: { kind: "workflow", action: "Approve" } })),
			{
				text: "Created Timesheet TS-0001",
				tone: "ok",
				open: { doctype: "Timesheet", name: "TS-0001" },
			},
		],
		[
			"S6 updated",
			row({ tool_name: "update_doc" }, ok({ doctype: "ToDo", name: "TD-9" })),
			{ text: "Updated ToDo TD-9", tone: "ok", open: { doctype: "ToDo", name: "TD-9" } },
		],
		[
			"S6 created with no name in the result: from the arguments, else none",
			row({}, { ok: true, data: {} }, { doctype: "ToDo" }),
			{ text: "Created ToDo", tone: "ok" },
		],
		[
			"S8 cancelled",
			row({ action_outcome: "discarded" }),
			{ text: "Draft cancelled", tone: "ok" },
		],
		[
			"S7 failed create",
			row({ action_outcome: "failed" }, fail({ message: "Employee is required." })),
			{ text: "Not created", tone: "error", reason: "Employee is required." },
		],
		[
			"S7 failed update",
			row(
				{ tool_name: "update_doc", action_outcome: "failed" },
				fail({ message: "Cannot edit." })
			),
			{ text: "Not saved", tone: "error", reason: "Cannot edit." },
		],
		[
			"S7 partly applied",
			row({ action_outcome: "partial" }, fail({ message: "Second step failed." })),
			{ text: "Partly applied", tone: "warn", reason: "Second step failed." },
		],
		[
			"S10 the record changed (reason code on the result)",
			row(
				{ tool_name: "update_doc", action_outcome: "failed" },
				fail({ message: "x" }, { reason_code: "stale" })
			),
			{ text: "Not saved: this record changed after the draft was made", tone: "error" },
		],
		[
			"S10 the record was deleted (reason code)",
			row(
				{ tool_name: "update_doc", action_outcome: "failed" },
				fail({ message: "x" }, { reason_code: "target_missing" })
			),
			{ text: "Not saved: this record changed after the draft was made", tone: "error" },
		],
		[
			"S10 reason code on the error",
			row({ tool_name: "update_doc", action_outcome: "failed" }, fail({ code: "stale" })),
			{ text: "Not saved: this record changed after the draft was made", tone: "error" },
		],
		[
			"S10 told only by the server's sentence",
			row(
				{ tool_name: "update_doc", action_outcome: "failed" },
				fail({ message: "The record changed after this action was proposed." })
			),
			{ text: "Not saved: this record changed after the draft was made", tone: "error" },
		],
		[
			"S10 from Save in the form: the draft's own sentence",
			row(
				{ tool_name: "update_doc", action_outcome: "failed" },
				fail({ message: "This record no longer exists, so the update was not saved." })
			),
			{ text: "Not saved: this record changed after the draft was made", tone: "error" },
		],
		[
			"a failure with no message has no reason",
			row({ action_outcome: "failed" }, null),
			{ text: "Not created", tone: "error", reason: "" },
		],
		[
			"a stored result that is not JSON is read as empty",
			row({ action_outcome: "failed", tool_result: "{oops" }),
			{ text: "Not created", tone: "error", reason: "" },
		],
		[
			"a result already parsed",
			row({ tool_result: ok(), tool_args: { doctype: "Timesheet" } }),
			{
				text: "Created Timesheet TS-0001",
				tone: "ok",
				open: { doctype: "Timesheet", name: "TS-0001" },
			},
		],
	];
	for (const [name, r, want] of table) it(name, () => expect(draftReceipt(r)).toEqual(want));
});

describe("draftReceipt: rows it leaves alone", () => {
	// Break: an outcome the State Table gives no receipt, or another tool's row, gets a draft
	// receipt (it would replace the chip the person has always seen).
	const none = [
		["replaced by a newer draft (S9)", row({ action_outcome: "superseded" })],
		["stopped before it was answered", row({ action_outcome: "cancelled" })],
		["expired", row({ action_outcome: "expired" })],
		["outcome unknown", row({ action_outcome: "unknown" })],
		["ran under an armed macro", row({ action_outcome: "auto_applied" })],
		["a value this build does not know", row({ action_outcome: "later" })],
		["no outcome (still pending, or the Claude route's twin)", row({ action_outcome: "" })],
		["another tool", row({ tool_name: "submit_doc" })],
		["not a tool row", row({ role: "assistant" })],
		["not a row", null],
	];
	for (const [name, r] of none) it(`${name}: null`, () => expect(draftReceipt(r)).toBeNull());
});

describe("isDraftReceiptRow: which decided rows were draft cards", () => {
	// Break: a flag-off row (no `draft_card` key) or another tool's row gets the draft wording, or a
	// draft row loses it after a reload (the site stamps every tool row when draft cards are on).
	const seen = new Set(["tok-1"]);
	const table = [
		[
			"the site stamped it (draft cards on, decided: false)",
			row({ draft_card: false }),
			new Set(),
			true,
		],
		["flag off: no stamp, not seen", row({}), new Set(), false],
		["no stamp but the chat showed this card", row({}), seen, true],
		["a seen token on another row", row({ tool_call_id: "tok-2" }), seen, false],
		["still pending (stamp true)", row({ draft_card: true, action_outcome: "" }), seen, false],
		[
			"another tool, stamped",
			row({ draft_card: false, tool_name: "submit_doc" }),
			seen,
			false,
		],
		["no outcome", row({ draft_card: false, action_outcome: "" }), seen, false],
		["no seen set", row({}), undefined, false],
		["not a row", undefined, seen, false],
	];
	for (const [name, r, set, want] of table)
		it(`${name}: ${want}`, () => expect(isDraftReceiptRow(r, set)).toBe(want));
});
