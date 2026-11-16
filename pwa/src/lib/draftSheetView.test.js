import { test } from "node:test";
import assert from "node:assert/strict";
import { errorLineOf, sheetTitle } from "./draftSheetView.js";

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
