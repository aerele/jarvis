import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { keptCardMessage, settledReasonMessage } from "./keptCard.js";

test("a refusal keeps the card unless it settled it, and says why", () => {
	for (const code of [
		"busy",
		"identity_refused",
		"armed_run",
		// approve_and_run (C5): the server keeps the response shape + one of
		// these on every refusal, and the card stays Pending on all of them.
		"not_runnable",
		"needs_own_confirm",
		"skill_not_armed",
		"storage_unavailable",
		"storage_outcome_unknown",
	]) {
		const words = keptCardMessage({ ok: false, reason_code: code });
		assert.match(words, /\w/, code);
		assert.notEqual(words, "This action could not be completed.", code);
	}
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

// D3/D2: cards never expire now, so an Approve (or Approve & run) on a card
// whose target changed settles it as Failed with a specific reason_code
// (stale/target_missing/tampered/unverifiable) alongside error.type
// InvalidConfirmation. keptCardMessage correctly treats that as settled (not
// kept), so the InvalidConfirmation branch must say the specific reason
// instead of the opaque "handled elsewhere" guess reserved for a bare legacy
// token (mirrors the SPA's chatSettledReason).
test("settledReasonMessage gives the specific reason for a card that settled as Failed", () => {
	assert.match(
		settledReasonMessage({
			ok: false,
			reason_code: "stale",
			pa_status: "Failed",
			error: { type: "InvalidConfirmation" },
		}),
		/record changed/
	);
	assert.match(
		settledReasonMessage({ ok: false, reason_code: "target_missing", pa_status: "Failed" }),
		/no longer exists/
	);
	assert.match(
		settledReasonMessage({ ok: false, reason_code: "tampered", pa_status: "Failed" }),
		/integrity check/
	);
	assert.match(
		settledReasonMessage({ ok: false, reason_code: "unverifiable", pa_status: "Failed" }),
		/verified/
	);
	assert.equal(settledReasonMessage({ ok: false, error: { type: "InvalidConfirmation" } }), "");
	assert.equal(settledReasonMessage({ ok: true }), "");
	assert.equal(settledReasonMessage(undefined), "");
});

test("DecisionSheet's approve() checks settledReasonMessage in its InvalidConfirmation branch", () => {
	const src = fs.readFileSync(
		new URL("../components/DecisionSheet.vue", import.meta.url),
		"utf8"
	);
	const body = src.slice(src.indexOf("async function approve("));
	const invalid = body.indexOf('r.error?.type === "InvalidConfirmation"');
	const reason = body.indexOf("settledReasonMessage(r)");
	assert.ok(invalid > -1);
	assert.ok(reason > invalid);
});
