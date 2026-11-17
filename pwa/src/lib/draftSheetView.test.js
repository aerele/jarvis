import { test } from "node:test";
import assert from "node:assert/strict";
import { errorLineOf, footerStatus, sheetTitle } from "./draftSheetView.js";

// Break: the header names the draft differently from the desktop panel ("New Timesheet", an update
// with its record name), or a model with no doctype prints "undefined".
test("the sheet's title", () => {
	assert.equal(sheetTitle({ verb: "create", doctype: "Timesheet" }), "New Timesheet");
	assert.equal(
		sheetTitle({ verb: "update", doctype: "Sales Order", docName: "SO-1" }),
		"Update Sales Order · SO-1"
	);
	assert.equal(sheetTitle({ verb: "create" }), "New");
	assert.equal(sheetTitle(null), "");
});

// Break: the error line drops the card's plain string (a refused plain Create from the form shows
// nothing), prints an envelope with no message, or shows "[object Object]".
test("the error line reads a string or an envelope, and nothing else", () => {
	assert.deepEqual(errorLineOf("Busy"), { message: "Busy", hint: "" });
	assert.deepEqual(errorLineOf({ message: " Bad ", hint: " Try later " }), {
		message: "Bad",
		hint: "Try later",
	});
	assert.deepEqual(errorLineOf({ message: "Bad", fields: [] }), { message: "Bad", hint: "" });
	for (const none of [null, undefined, "", "  ", {}, { message: " " }, { hint: "x" }])
		assert.equal(errorLineOf(none), null);
});

// Break: a failed Save is only said in the banner at the top of a scrolling body (out of sight
// beside the button the person just tapped), a dead card is not said in the footer, or a refusal
// that marks fields prints the first message instead of pointing at the marks.
test("the footer's one line", () => {
	const line = { message: "Jarvis could not reach the server.", hint: "" };
	assert.equal(footerStatus({ errorLine: null, dead: false, deadText: "Dead." }), null);
	assert.deepEqual(footerStatus({ errorLine: line, dead: false, deadText: "Dead." }), {
		text: "Jarvis could not reach the server.",
		tone: "error",
	});
	assert.deepEqual(
		footerStatus({ errorLine: line, dead: false, deadText: "Dead.", marked: true }),
		{
			text: "Fix the marked fields, then save again.",
			tone: "error",
		}
	);
	// a dead card outranks an old error
	assert.deepEqual(footerStatus({ errorLine: line, dead: true, deadText: "Dead." }), {
		text: "Dead.",
		tone: "error",
	});
});
