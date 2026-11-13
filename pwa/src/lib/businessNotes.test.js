// #747: deleting a note on the Business screen left "your notes" at its old count
// until a reload. The count is the status's (getBusinessStatus), so a delete moves it.
//
// No component harness in this app (see pumpFence.test.js), so the view's wiring is
// asserted against its source, as macroClosedRender.test.js does.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { statusAfterDelete } from "./businessNotes.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const viewSrc = fs.readFileSync(path.join(HERE, "..", "views", "BusinessView.vue"), "utf8");

test("a delete takes one off the count, never below zero", () => {
	assert.deepEqual(statusAfterDelete({ my_notes: 1, stt_enabled: true }), {
		my_notes: 0,
		stt_enabled: true,
	});
	assert.equal(statusAfterDelete({ my_notes: 5 }).my_notes, 4);
	assert.equal(statusAfterDelete({ my_notes: 0 }).my_notes, 0);
	assert.equal(statusAfterDelete({}).my_notes, 0);
	assert.equal(statusAfterDelete(null), null);
});

function removeBody() {
	const start = viewSrc.indexOf("async function remove(name) {");
	assert.notEqual(start, -1, "BusinessView must still define remove(name)");
	const end = viewSrc.indexOf("\n}\n", start);
	return viewSrc.slice(start, end);
}

test("remove moves the count only after the delete succeeded, then re-reads it", () => {
	const body = removeBody();
	const del = body.indexOf("await api.deleteVoiceNote(name)");
	const count = body.indexOf("status.value = statusAfterDelete(status.value)");
	const refresh = body.indexOf("refreshStatus()");
	const failed = body.indexOf("catch (e)");
	assert.ok(del > -1 && count > del, "the count moves after the delete call");
	assert.ok(refresh > count, "and the server's count is read back");
	assert.ok(count < failed, "a failed delete (the catch) does not touch the count");
});

test("a second tap on a note being deleted does nothing", () => {
	assert.match(removeBody(), /if \(deleting\.value\.has\(name\)\) return;/);
	assert.match(viewSrc, /:disabled="deleting\.has\(n\.name\)"/);
});
