import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { keptCardMessage } from "./keptCard.js";

test("a refusal keeps the card unless it settled it, and says why", () => {
	for (const code of ["busy", "identity_refused", "armed_run"])
		assert.match(keptCardMessage({ ok: false, reason_code: code }), /\w/, code);
	assert.equal(
		keptCardMessage({ ok: false, reason_code: "executing", pa_status: "Executing" }),
		"This action is already running."
	);
	for (const res of [
		{ ok: true },
		{ ok: false, reason_code: "already_handled" },
		{ ok: false, reason_code: "not_found" },
		{ ok: false, reason_code: "failed", pa_status: "Failed" },
		{ ok: false, reason_code: "stale", pa_status: "Failed" },
		{ ok: false, error: { type: "InvalidConfirmation" } },
		{ ok: false, error: { type: "ValidationError", message: "legacy tool failed" } },
		undefined,
	])
		assert.equal(keptCardMessage(res), "");
});

test("DecisionSheet checks it before treating the card as spent", () => {
	const src = fs.readFileSync(
		new URL("../components/DecisionSheet.vue", import.meta.url),
		"utf8"
	);
	const body = src.slice(src.indexOf("async function approve("));
	const kept = body.indexOf("keptCardMessage(r)");
	assert.ok(kept > -1);
	assert.ok(kept < body.indexOf('r.error?.type === "InvalidConfirmation"'));
});
