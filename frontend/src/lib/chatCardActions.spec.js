import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	chatOutcomeMessage,
	chatRefusalMessage,
	chatSettledReason,
	chatStatusLine,
	confirmReplyOutcome,
	dismissReplyOutcome,
	isChatSettled,
	keepsChatCard,
	nextStepActedKeys,
	nextStepRefusal,
	shouldHideNextStep,
} from "./chatCardActions";

describe("chatCardActions copy", () => {
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
		expect(chatRefusalMessage({ ok: false, reason_code: "storage_unavailable" })).toBe(
			"Couldn't reach confirmation storage. Try again in a moment."
		);
		expect(chatRefusalMessage({ ok: false, reason_code: "storage_outcome_unknown" })).toBe(
			"Lost track of whether that went through. Check before retrying."
		);
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
		expect(isChatSettled({ ok: false, reason_code: "safety_update" })).toBe(true);
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
			{ ok: false, reason_code: "safety_update" },
			{ ok: false, reason_code: "failed", pa_status: "Failed" },
			{ ok: false, reason_code: "stale", pa_status: "Failed" },
			{ ok: false, error: { type: "InvalidConfirmation" } },
			{ ok: false, error: { type: "ValidationError", message: "legacy tool failed" } },
			null,
		])
			expect(keepsChatCard(res)).toBe(false);
	});
});

describe("ChatView direct safety-update decisions", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	const actionBody = (name) => {
		const start = src.indexOf(`async function ${name}(`);
		const next = src.indexOf("\nasync function ", start + 1);
		return src.slice(start, next === -1 ? undefined : next);
	};

	// The notice slice ends at a comment the view keeps: a missing marker would make the slice the
	// rest of the file and the checks below would pass for the wrong reason.
	it("settles a safety update by its token only, and says it once", () => {
		const start = src.indexOf("function settleSafetyUpdateNotice(token, message)");
		const end = src.indexOf("// A direct Confirm, Approve & run, or Discard can race");
		expect(start).toBeGreaterThan(-1);
		expect(end).toBeGreaterThan(start);
		const notice = src.slice(start, end);
		expect(notice).toContain("safetySettledTokens.value");
		expect(notice).toContain("removePending(token)");
		expect(notice).toContain("if (!seen)");
		expect(notice).toContain("enqueueSafetyUpdateNotice");
		// Filtering by the response token removes the cancelled card only. The
		// visible queue still contains an independent safe sibling.
		expect(src).toContain(
			"pendingActions.value = pendingActions.value.filter((x) => x.token !== token)"
		);
	});

	// Break: the ladder takes a safety update for a retryable refusal (it is terminal: the card is
	// spent), or reads it after the storage and keep branches. The rows are executed here; the
	// effects the desktop does with `safety` are in chatCardDoors.spec.js.
	it("makes safety_update terminal before any retryable card branch", () => {
		const withStorageShape = {
			ok: false,
			reason_code: "safety_update",
			error: { type: "ConfirmationUnavailableError", message: "m" },
		};
		expect(confirmReplyOutcome(withStorageShape).safety).toEqual({ message: "m" });
		expect(dismissReplyOutcome(withStorageShape).safety).toEqual({ message: "m" });
		expect(confirmReplyOutcome(withStorageShape).fate).toBe("gone");
	});

	it("applies a safety outcome by settling the notice, before any other branch", () => {
		const body = src.slice(
			src.indexOf("async function applyCardOutcome("),
			src.indexOf("async function confirmPending(")
		);
		expect(body.indexOf("out.safety")).toBeGreaterThan(-1);
		expect(
			body.indexOf("settleSafetyUpdateNotice(token, out.safety.message)")
		).toBeGreaterThan(-1);
		expect(body.indexOf("out.safety")).toBeLessThan(body.indexOf('out.fate === "kept"'));
		expect(body.indexOf('out.fate === "kept"')).toBeLessThan(
			body.indexOf("removePending(token)")
		);
	});

	it("captures the view, conversation and token before every direct decision", () => {
		for (const [fn, rpc] of [
			["confirmPending", "api.confirmTool(token, view.conversation)"],
			["approveAndRunPending", "api.approveAndRun(token, view.conversation)"],
			["discardPending", "api.dismissTool(token, view.conversation)"],
		]) {
			const body = actionBody(fn);
			expect(body, fn).toContain("const view = captureChatView(token);");
			expect(body, fn).toContain("pa.conversation !== view.conversation");
			expect(body, fn).toContain(rpc);
			expect(body, fn).toContain("if (!pendingActionIsCurrent(view)) return;");
		}
		// after the reload the outcome's host checks the view again, before it refreshes the list
		const apply = actionBody("applyCardOutcome");
		expect(apply.indexOf("await loadConversation(view.conversation)")).toBeLessThan(
			apply.indexOf("if (!chatViewIsCurrent(view)) return;")
		);
		expect(apply.indexOf("if (!chatViewIsCurrent(view)) return;")).toBeLessThan(
			apply.indexOf("store.loadConversations()")
		);
	});
});

// Break: a handler decides a reply itself again (a second ladder beside the shared one: the two
// drift, as the phone's copy did), or reads the wrong door's decision. What each reply does is
// executed in chatCardDoors.spec.js (the effects) and chatCardDecision.spec.js (the table).
describe("ChatView's handlers use the shared decision and nothing else", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	const body = (fn) => {
		const start = src.indexOf(`async function ${fn}(`);
		return src.slice(start, src.indexOf("\nasync function ", start + 1));
	};
	for (const [fn, decide, thrownDoor] of [
		["confirmPending", "confirmReplyOutcome(r,", "confirm"],
		["approveAndRunPending", "confirmReplyOutcome(r,", "approve"],
		["discardPending", "dismissReplyOutcome(r)", "dismiss"],
	]) {
		it(`${fn} applies ${decide} and reads a thrown call with thrownCardError(e, "${thrownDoor}")`, () => {
			const text = body(fn);
			expect(text).toContain(`applyCardOutcome(`);
			expect(text).toContain(decide);
			expect(text).toContain(`thrownCardError(e, "${thrownDoor}")`);
			for (const own of [
				"keepsChatCard(",
				"chatSettledReason(",
				"chatRefusalMessage(",
				"confirmationStorageUnavailable(",
				"r.ok === false",
				"r.error",
				"thrownActionError(",
			])
				expect(text, own).not.toContain(own);
		});
	}
});

describe("chatSettledReason", () => {
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
	for (const fn of ["onTypedConfirmResolved"]) {
		it(`${fn} checks chatSettledReason in its InvalidConfirmation branch`, () => {
			const start = src.indexOf(`async function ${fn}(`);
			const body = src.slice(start, src.indexOf("\nasync function ", start + 1));
			const invalid = body.indexOf('r.error.type === "InvalidConfirmation"');
			const reason = body.indexOf("chatSettledReason(r");
			expect(invalid).toBeGreaterThan(-1);
			expect(reason).toBeGreaterThan(invalid);
		});
	}
});

describe("chatRefusalMessage prefers the person's words", () => {
	it("shows error.person_message over the model-facing message", () => {
		const res = {
			ok: false,
			error: {
				code: "sensitive_refused",
				message:
					"... Do not retry it with another tool; tell the user to use Data Import in Desk.",
				person_message: "This import cannot be run from chat. Use Data Import in Desk.",
			},
		};
		expect(chatRefusalMessage(res)).toBe(
			"This import cannot be run from chat. Use Data Import in Desk."
		);
		expect(chatRefusalMessage({ ok: false, error: { message: "Plain" } })).toBe("Plain");
	});
});

describe("next-step refusals (propose_next_action)", () => {
	const modelText = "a card is waiting. Do NOT retry this call; stop and end your turn now.";
	it("keeps the button and shows the person's words for a temporary refusal", () => {
		const pending = {
			ok: false,
			error: {
				code: "ConfirmationPendingError",
				message: modelText,
				person_message: "Finish or discard the open card first.",
			},
		};
		const r = nextStepRefusal(pending);
		expect(r.temporary).toBe(true);
		expect(r.message).toBe("Finish or discard the open card first.");
		const down = nextStepRefusal({
			ok: false,
			error: { code: "ConfirmationUnavailableError", message: modelText },
		});
		expect(down.temporary).toBe(true);
		expect(down.message).not.toContain("do NOT retry");
		expect(down.message).toBe("Couldn't open the card right now. Try again.");
	});
	it("hides the step for a final refusal, never with model-facing text", () => {
		const r = nextStepRefusal({ ok: false, error: { code: "Other", message: modelText } });
		expect(r.temporary).toBe(false);
		expect(r.message).toBe("That next step is not available.");
	});
	it("keeps the step for a temporary refusal and hides it for a final one", () => {
		const temp = nextStepRefusal({ ok: false, error: { code: "ConfirmationPendingError" } });
		const final = nextStepRefusal({ ok: false, error: { code: "Other" } });
		expect(shouldHideNextStep(temp)).toBe(false);
		expect(shouldHideNextStep(final)).toBe(true);
	});
	it("counts confirmed and auto_applied receipts as acted, nothing else", () => {
		const row = (tool_name, action_outcome, name = "SO-1") => ({
			role: "tool",
			tool_name,
			action_outcome,
			tool_args: JSON.stringify({ doctype: "Sales Order", name }),
		});
		const acted = (r) => nextStepActedKeys([r]).has("Sales Order|SO-1");
		expect(acted(row("submit_doc", "confirmed"))).toBe(true);
		expect(acted(row("submit_doc", "auto_applied"))).toBe(true);
		expect(acted(row("apply_workflow_action", "auto_applied"))).toBe(true);
		for (const o of ["failed", "discarded", "unknown", "partial", "cancelled", ""]) {
			expect(acted(row("submit_doc", o))).toBe(false);
		}
		expect(acted(row("create_doc", "confirmed"))).toBe(false);
		expect(nextStepActedKeys([], new Set(["A|b"])).has("A|b")).toBe(true);
	});
});
