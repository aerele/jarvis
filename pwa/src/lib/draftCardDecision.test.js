import { test } from "node:test";
import assert from "node:assert/strict";
import {
	CANCEL_FAILED_TEXT,
	CREATE_FAILED_TEXT,
	DRAFT_STALE_TEXT,
	cancelOutcome,
	createOutcome,
	thrownOutcome,
} from "./draftCardDecision.js";

const gone = { gone: true, error: "", say: "" };
const kept = (error) => ({ gone: false, error, say: "" });
const INVALID = { type: "InvalidConfirmation" };

// What the phone does with confirm_tool's answer, one row per reply the server sends. Each row
// names the break it catches (a wrong card fate: a spent card left to be pressed again, or a card
// that is still pending taken away).
const CREATE = [
	["success", { ok: true, name: "TS-1" }, gone],
	["no body", undefined, gone],
	// the card is still Pending
	[
		"busy",
		{ ok: false, reason_code: "busy" },
		kept("This action is being handled right now. Try again in a moment."),
	],
	[
		"executing",
		{ ok: false, reason_code: "executing" },
		kept("This action is already running."),
	],
	[
		"identity refused",
		{ ok: false, reason_code: "identity_refused" },
		kept("This action can no longer run as the user it was proposed for. Nothing ran."),
	],
	[
		"storage down",
		{ ok: false, reason_code: "storage_unavailable" },
		kept("Couldn't reach confirmation storage. Try again in a moment."),
	],
	[
		"unknown pending reason with a message",
		{ ok: false, reason_code: "weird", error: { message: "Server says no" } },
		kept("Server says no"),
	],
	// the card is spent
	["safety update", { ok: false, reason_code: "safety_update" }, gone],
	[
		"tool failed (receipt follows)",
		{ ok: false, reason_code: "failed", pa_status: "Failed" },
		gone,
	],
	["spent legacy token, no reason", { ok: false, error: { type: "ValidationError" } }, gone],
	[
		"record changed",
		{ ok: false, reason_code: "stale", pa_status: "Failed", error: INVALID },
		{ gone: true, error: "", say: DRAFT_STALE_TEXT },
	],
	[
		"record deleted",
		{ ok: false, reason_code: "target_missing", pa_status: "Failed", error: INVALID },
		{ gone: true, error: "", say: "The record this targets no longer exists. Nothing ran." },
	],
	[
		"token gone, no reason",
		{ ok: false, error: INVALID },
		{
			gone: true,
			error: "",
			say: "Couldn't confirm. It may have been handled elsewhere. Refresh, or ask Jarvis to try again.",
		},
	],
];
for (const [name, reply, expected] of CREATE)
	test(`Create: ${name}`, () => assert.deepEqual(createOutcome(reply), expected));

test("Create: the assistant's own name is used in the elsewhere line", () => {
	assert.match(
		createOutcome({ ok: false, error: INVALID }, { agent: "Tara" }).say,
		/ask Tara to try/
	);
});

// Break: a refused Cancel takes the card away (it is still Pending), or a done one leaves it.
const CANCEL = [
	["success", { ok: true }, gone],
	["already handled (benign no-op)", { ok: true, status: "already_handled" }, gone],
	["safety update", { ok: false, reason_code: "safety_update" }, gone],
	[
		"storage error with a message",
		{ ok: false, error: { message: "Storage is down" } },
		kept("Storage is down"),
	],
	["refused with only a reason", { ok: false, reason: "Not allowed" }, kept("Not allowed")],
	["refused with nothing", { ok: false }, kept(CANCEL_FAILED_TEXT)],
];
for (const [name, reply, expected] of CANCEL)
	test(`Cancel: ${name}`, () => assert.deepEqual(cancelOutcome(reply), expected));

// Break: a thrown call (offline) removes the card, or shows the other button's fallback text.
test("a thrown call keeps the card with the error's own message or the button's fallback", () => {
	assert.deepEqual(
		thrownOutcome("create", new Error("Failed to fetch")),
		kept("Failed to fetch")
	);
	assert.deepEqual(thrownOutcome("create", {}), kept(CREATE_FAILED_TEXT));
	assert.deepEqual(thrownOutcome("cancel", null), kept(CANCEL_FAILED_TEXT));
});
