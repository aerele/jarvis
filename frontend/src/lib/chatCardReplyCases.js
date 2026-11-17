// The replies the server gives to Create / Confirm (confirm_tool, approve_and_run) and to Cancel /
// Discard (dismiss_tool) of a pending card, and the errors a call can throw. Real envelopes: each
// row says in `server` where in jarvis/chat the shape comes from, so a test never pins a shape the
// server does not send (a phone test once did: `reason_code: "storage_unavailable"` on a plain
// confirm, which the real storage reply never carries; it has `error.type` and no `reason_code`).
//
// One data file for every runner: chatCardDoors.spec.js executes the desktop's handlers over it
// (the effect golden), chatCardDecision.spec.js runs the shared decision over it (the reply table),
// and the phone spec runs the phone's host over it. Not part of the app bundle.
//
// A row is { id, doors, reply | thrown, server, [expires] }:
//   doors    the handlers that can receive this reply: "confirm" (confirmPending), "approve"
//            (approveAndRunPending), "dismiss" (discardPending)
//   expires  "past" | "future": the card's own expiry stamp (only the bare "no longer valid"
//            reply reads it)
//   thrown   { message, status?, messages?, exc_type?, kind? }: the call rejects with an error
//            built from it (see `thrownError`)

const CONFIRM = ["confirm", "approve"];
const APPROVE = ["approve"];
const DISMISS = ["dismiss"];

// ---- the envelopes, as the server builds them --------------------------------------------------

// pending_actions/_execute.py `_refusal(code, message, **extra)`.
const refusal = (reason_code, message, extra = {}) => ({
	ok: false,
	error: { type: "InvalidConfirmation", message },
	reason_code,
	...extra,
});

const SAFETY_TEXT =
	"This confirmation was cancelled during an agent safety update. Submit the action again if it is still needed.";

// actions_api.py _CONFIRMATION_UNAVAILABLE / _CONFIRMATION_OUTCOME_UNKNOWN (~1325-1347): NO reason_code.
const STORAGE_UNAVAILABLE = {
	ok: false,
	error: {
		type: "ConfirmationUnavailableError",
		message:
			"Confirmations are temporarily unavailable. Nothing was changed. Keep this card and try again in a moment.",
	},
};
const STORAGE_UNKNOWN = {
	ok: false,
	error: {
		type: "ConfirmationOutcomeUnknownError",
		message:
			"We couldn't confirm whether that went through. Nothing was changed by this request. Reload the chat to see the current cards, then try again.",
	},
};

export const REPLY_ROWS = [
	// ---- Create / Confirm / Approve & run ------------------------------------------------------
	{
		id: "ok",
		doors: CONFIRM,
		reply: {
			ok: true,
			data: { doctype: "Timesheet", name: "TS-0001" },
			reason_code: null,
			pa_status: "Executed",
			outcome: "confirmed",
		},
		server: "pending_actions/_execute.py _claim_and_run: response.update(reason_code, pa_status, outcome)",
	},
	{
		id: "ok, the continuation turn queued (a draft that continues)",
		doors: CONFIRM,
		reply: {
			ok: true,
			data: { doctype: "Timesheet", name: "TS-0002" },
			reason_code: null,
			pa_status: "Executed",
			outcome: "confirmed",
			queued: true,
			queued_position: 2,
			run_id: "run-1",
			message_id: "msg-1",
		},
		server: "actions_api.py _thread_queued (~1660)",
	},
	{
		id: "ok, queued with no position",
		doors: CONFIRM,
		reply: { ok: true, reason_code: null, queued: true, run_id: "run-2", message_id: "msg-2" },
		server: "actions_api.py _thread_queued: queued_position is cont.get(...), which can be None",
	},
	{
		id: "no reply at all",
		doors: CONFIRM,
		reply: null,
		server: "client: the call resolved with nothing",
	},
	{
		id: "failed after the card was spent (the tool refused)",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: { code: "InvalidArgumentError", message: "Due Date is required." },
			reason_code: "failed",
			pa_status: "Failed",
			outcome: "failed",
		},
		server: "_execute.py _claim_and_run (status FAILED, code failed) + _responses.err: error has `code`, not `type`",
	},
	{
		id: "partly applied",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: { code: "InternalError", message: "The confirmed action may have partly run." },
			reason_code: "partial",
			pa_status: "Failed",
			outcome: "partial",
		},
		server: "_execute.py _claim_and_run (code partial)",
	},
	{
		id: "the execute call crashed (no reason_code)",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: {
				code: "InternalError",
				message: "the confirmed action hit an unexpected error; check before retrying",
			},
		},
		server: "actions_api.py _run_pending_action: except ExecuteCrashed -> api._error(InternalError)",
	},
	{
		id: "safety update (a pending action)",
		doors: CONFIRM,
		reply: {
			...refusal("safety_update", SAFETY_TEXT),
			pa_status: "Cancelled",
			outcome: "cancelled",
		},
		server: "_execute.py _cancel_unrun_safety_update: _refusal(*_SAFETY_UPDATE, pa_status, outcome)",
	},
	{
		id: "safety update (a revoked legacy token)",
		doors: CONFIRM,
		reply: refusal("safety_update", SAFETY_TEXT),
		server: "actions_api.py _confirmation_storage_error: {**_CONFIRMATION_REVOKED, reason_code: safety_update}",
	},
	{
		id: "safety update with no message",
		doors: CONFIRM,
		reply: { ok: false, reason_code: "safety_update" },
		server: "defensive: the handlers read error.message with a fallback",
	},
	{
		id: "storage unavailable (confirm_tool)",
		doors: CONFIRM,
		reply: STORAGE_UNAVAILABLE,
		server: "actions_api.py _confirm_core: _confirmation_storage_error -> _CONFIRMATION_UNAVAILABLE (no reason_code)",
	},
	{
		id: "storage outcome unknown (confirm_tool)",
		doors: CONFIRM,
		reply: STORAGE_UNKNOWN,
		server: "actions_api.py _confirm_core: _CONFIRMATION_OUTCOME_UNKNOWN (no reason_code)",
	},
	{
		id: "storage unavailable (approve_and_run adds a reason_code)",
		doors: APPROVE,
		reply: { ...STORAGE_UNAVAILABLE, reason_code: "storage_unavailable" },
		server: "actions_api.py approve_and_run (~1749): {**res, reason_code: storage_unavailable}",
	},
	{
		id: "storage outcome unknown (approve_and_run adds a reason_code)",
		doors: APPROVE,
		reply: { ...STORAGE_UNKNOWN, reason_code: "storage_outcome_unknown" },
		server: "actions_api.py approve_and_run (~1749): reason_code storage_outcome_unknown",
	},
	{
		id: "storage unavailable with no message",
		doors: CONFIRM,
		reply: { ok: false, error: { type: "ConfirmationUnavailableError" } },
		server: "defensive: the card gets the error object, the toast gets undefined",
	},
	{
		id: "busy (the row is locked)",
		doors: CONFIRM,
		reply: refusal("busy", "This action is being handled right now. Try again in a moment."),
		server: "_execute.py _BUSY refusal (no pa_status)",
	},
	{
		id: "busy (a guarded structure form is locked)",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: {
				code: "RetryLaterError",
				message: "That form is being changed. Try again.",
				hint: "Try again in a moment.",
				kind: "retry_later",
			},
			reason_code: "busy",
			pa_status: "Pending",
			outcome: null,
		},
		server: "_execute.py _busy_structure",
	},
	{
		id: "already executing",
		doors: CONFIRM,
		reply: refusal("executing", "This confirmation is no longer valid.", {
			pa_status: "Executing",
			outcome: null,
		}),
		server: "_execute.py handled(row): code executing, pa_status Executing",
	},
	{
		id: "identity refused",
		doors: CONFIRM,
		reply: refusal(
			"identity_refused",
			"This action can no longer run as the user it was proposed for. Nothing ran."
		),
		server: "_execute.py _IDENTITY refusal",
	},
	{
		id: "an armed macro run is stopping",
		doors: CONFIRM,
		reply: refusal(
			"armed_run",
			"This macro run is stopping; that confirmation was withdrawn and nothing ran."
		),
		server: "_execute.py _ARMED refusal",
	},
	{
		id: "approve and run offered no more (not_runnable)",
		doors: APPROVE,
		reply: {
			ok: false,
			reason_code: "not_runnable",
			error: {
				type: "InvalidConfirmation",
				message:
					"This card can't be approved as a run - confirm the step on its own instead. Nothing was changed.",
			},
		},
		server: "actions_api.py _APPROVE_RUN_NOT_RUNNABLE (~1364)",
	},
	{
		id: "approve and run: the skill is not armed",
		doors: APPROVE,
		reply: {
			ok: false,
			reason_code: "skill_not_armed",
			error: {
				type: "InvalidConfirmation",
				message: "This skill is no longer set up for Approve & run.",
			},
		},
		server: "actions_api.py _APPROVE_RUN_NOT_ARMED",
	},
	{
		id: "approve and run: the step needs its own confirmation",
		doors: APPROVE,
		reply: {
			ok: false,
			reason_code: "needs_own_confirm",
			error: {
				type: "InvalidConfirmation",
				message: "This step needs its own confirmation.",
			},
		},
		server: "actions_api.py _APPROVE_RUN_NEVER_TOOL",
	},
	{
		id: "a refusal code the client does not know",
		doors: CONFIRM,
		reply: refusal("future_code", "Something the server says in its own words."),
		server: "forward compatibility: keepsChatCard keeps any unsettled reason_code",
	},
	{
		id: "not found (the token is gone)",
		doors: CONFIRM,
		reply: refusal("not_found", "This confirmation is no longer valid."),
		server: "_execute.py _NOT_FOUND refusal",
	},
	{
		id: "already handled elsewhere (confirmed)",
		doors: CONFIRM,
		reply: refusal("already_handled", "This confirmation is no longer valid.", {
			pa_status: "Executed",
			outcome: "confirmed",
		}),
		server: "_execute.py handled(row) on a terminal row",
	},
	{
		id: "already handled elsewhere (discarded)",
		doors: CONFIRM,
		reply: refusal("already_handled", "This confirmation is no longer valid.", {
			pa_status: "Discarded",
			outcome: "discarded",
		}),
		server: "_execute.py handled(row) on a terminal row",
	},
	{
		id: "stale: the record changed after it was proposed",
		doors: CONFIRM,
		reply: refusal("stale", "The action could not be applied.", {
			pa_status: "Failed",
			outcome: "failed",
		}),
		server: "_execute.py _fail_unrun(row, approver, 'stale'): _refusal(code, REASON_TEXT[code], pa_status=FAILED)",
	},
	{
		id: "target missing: the record is gone",
		doors: CONFIRM,
		reply: refusal("target_missing", "The record this action targets no longer exists.", {
			pa_status: "Failed",
			outcome: "failed",
		}),
		server: "_execute.py _fail_unrun(row, approver, 'target_missing')",
	},
	{
		id: "tampered",
		doors: CONFIRM,
		reply: refusal("tampered", "This action failed an integrity check.", {
			pa_status: "Failed",
			outcome: "failed",
		}),
		server: "_execute.py _fail_unrun(row, approver, 'tampered')",
	},
	{
		id: "unverifiable",
		doors: CONFIRM,
		reply: refusal("unverifiable", "This action can no longer be verified on this site.", {
			pa_status: "Failed",
			outcome: "failed",
		}),
		server: "_execute.py _fail_unrun(row, approver, 'unverifiable')",
	},
	{
		id: "a used legacy token, the card has no expiry",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: {
				type: "InvalidConfirmation",
				message: "This confirmation is no longer valid.",
			},
		},
		server: "actions_api.py _INVALID_CONFIRM (no reason_code)",
	},
	{
		id: "a used legacy token, the card's expiry has passed",
		doors: CONFIRM,
		expires: "past",
		reply: {
			ok: false,
			error: {
				type: "InvalidConfirmation",
				message: "This confirmation is no longer valid.",
			},
		},
		server: "actions_api.py _INVALID_CONFIRM; the card's own expires_at decides the words",
	},
	{
		id: "a used legacy token, the card's expiry is ahead",
		doors: CONFIRM,
		expires: "future",
		reply: {
			ok: false,
			error: {
				type: "InvalidConfirmation",
				message: "This confirmation is no longer valid.",
			},
		},
		server: "actions_api.py _INVALID_CONFIRM",
	},
	{
		id: "an armed run withdrew the confirmation (no reason_code)",
		doors: CONFIRM,
		reply: {
			ok: false,
			error: {
				type: "InvalidConfirmation",
				message:
					"This automated run is stopping, so that confirmation was withdrawn. Nothing was changed.",
			},
		},
		server: "actions_api.py _ARMED_RUN_CONFIRM_WITHDRAWN",
	},
	{
		id: "a body that says ok:true but names a failure code",
		doors: CONFIRM,
		reply: { ok: true, reason_code: "stale" },
		server: "edge: only ok === false is a refusal",
	},

	// ---- Cancel / Discard (dismiss_tool) -------------------------------------------------------
	{
		id: "discarded",
		doors: DISMISS,
		reply: {
			ok: true,
			data: { status: "discarded", tool: "create_doc" },
			reason_code: "discarded",
		},
		server: "pending_actions/_lifecycle.py discard (last line)",
	},
	{
		id: "already handled (discard of a spent card)",
		doors: DISMISS,
		reply: { ok: true, data: { status: "already_handled" }, reason_code: "already_handled" },
		server: "actions_api.py _dismiss_core / _discard_pending_action",
	},
	{
		id: "no reply at all (discard)",
		doors: DISMISS,
		reply: undefined,
		server: "client: the call resolved with nothing",
	},
	{
		id: "discard refused: busy",
		doors: DISMISS,
		reply: refusal("busy", "This action is being handled right now. Try again in a moment."),
		server: "_lifecycle.py discard: _refusal('busy', ...) when the row is locked",
	},
	{
		id: "discard refused: already executing",
		doors: DISMISS,
		reply: refusal("executing", "This action is already running; it can't be discarded.", {
			pa_status: "Executing",
		}),
		server: "_lifecycle.py discard: _refusal('executing', ..., pa_status=EXECUTING)",
	},
	{
		id: "discard: storage unavailable",
		doors: DISMISS,
		reply: STORAGE_UNAVAILABLE,
		server: "actions_api.py _dismiss_core: _confirmation_storage_error (no reason_code)",
	},
	{
		id: "discard: storage outcome unknown",
		doors: DISMISS,
		reply: STORAGE_UNKNOWN,
		server: "actions_api.py _dismiss_core: _CONFIRMATION_OUTCOME_UNKNOWN",
	},
	{
		id: "discard: safety update",
		doors: DISMISS,
		reply: { ...refusal("safety_update", SAFETY_TEXT), pa_status: "Cancelled" },
		server: "actions_api.py _confirmation_storage_error (revoked legacy token) through _dismiss_core",
	},
	{
		id: "discard: ok:true that carries the safety code",
		doors: DISMISS,
		reply: { ok: true, reason_code: "safety_update" },
		server: "edge: discardPending checks the safety code outside the ok === false gate",
	},
	{
		id: "discard: a refusal with no reason_code",
		doors: DISMISS,
		reply: { ok: false, error: { code: "InternalError", message: "Could not discard." } },
		server: "edge: no reason_code means a spent card, the card goes",
	},
	{
		id: "discard: not found (raw)",
		doors: DISMISS,
		reply: refusal("not_found", "This confirmation is no longer valid."),
		server: "_lifecycle.py discard returns it before actions_api turns it into already_handled",
	},
	{
		id: "discard: a refusal code the client does not know",
		doors: DISMISS,
		reply: refusal("future_code", "Something the server says in its own words."),
		server: "forward compatibility",
	},

	// ---- a call that throws ---------------------------------------------------------------------
	{
		id: "thrown: a plain error",
		doors: [...CONFIRM, ...DISMISS],
		thrown: { message: "boom" },
		server: "any rejected call",
	},
	{
		id: "thrown: the server said why (417)",
		doors: [...CONFIRM, ...DISMISS],
		thrown: {
			message: "ValidationError: x",
			status: 417,
			messages: ["Due Date is required."],
		},
		server: "frappe.throw through frappe-ui call()",
	},
	{
		id: "thrown: the server said why but it was a 500",
		doors: [...CONFIRM, ...DISMISS],
		thrown: { message: "ServerError", status: 500, messages: ["Oops"] },
		server: "an unexpected exception",
	},
	{
		id: "thrown: offline",
		doors: [...CONFIRM, ...DISMISS],
		thrown: { message: "Failed to fetch", kind: "TypeError" },
		server: "browser fetch failure",
	},
	{
		id: "thrown: the session expired",
		doors: [...CONFIRM, ...DISMISS],
		thrown: { message: "Unauthorized", status: 401 },
		server: "frappe session cleared",
	},
	{
		id: "thrown: a refused permission on a live session",
		doors: [...CONFIRM, ...DISMISS],
		thrown: {
			message: "PermissionError",
			status: 403,
			exc_type: "PermissionError",
			messages: ["No permission for Timesheet"],
		},
		server: "frappe.PermissionError",
	},
];

// An error as frappe-ui's call() rejects with it.
export function thrownError(spec) {
	const e = spec.kind === "TypeError" ? new TypeError(spec.message) : new Error(spec.message);
	for (const key of ["status", "messages", "exc_type"]) if (key in spec) e[key] = spec[key];
	return e;
}

export const rowsOf = (door) => REPLY_ROWS.filter((r) => r.doors.includes(door));

// ---- what the decision must say for each row ---------------------------------------------------
//
// Written by hand from the product's rules (the desktop's texts are the source of truth), NOT
// generated from the code: chatCardDoors.golden.json says what the old handlers did, this says what
// the shared decision (lib/chatCardActions.js) must return. Both the desktop and the phone apply it.
//   fate      "gone" (take the card off) or "kept" (it is still pending; `message` shows with it)
//   message   the words for the person; "" when the receipt in the reloaded thread speaks
//   toast     the desktop also shows `message` as an error toast
//   reload    the desktop reloads the chat after taking the card off (the phone always does)
//   cardError "$error" = the reply's own error object (a storage reply), "$message" = { message },
//             null = none
//   queued / safety  as chatCardActions documents them
//   draft     what differs for a server draft card
// A thrown row has `thrown` = { message } (a plain message) or { internal: true } (the door's
// fallback, on an object that carries InternalError).
const T = {
	busy: "This action is being handled right now. Try again in a moment.",
	executing: "This action is already running.",
	identity: "This action can no longer run as the user it was proposed for. Nothing ran.",
	armed: "This macro run is stopping; the action was withdrawn and nothing ran.",
	notRunnable: "This card no longer offers Approve & run. Use Confirm instead.",
	notArmed:
		"The skill this would run is no longer armed, or was switched off. Use Confirm instead.",
	ownConfirm:
		"This action must be confirmed on its own, not run automatically. Use Confirm instead.",
	notFound: "This action is no longer waiting.",
	handled: "This action was already handled.",
	stale: "The record changed after this was proposed. Nothing ran.",
	draftStale: "This record changed after the draft was made. Nothing was saved.",
	missing: "The record this targets no longer exists. Nothing ran.",
	tampered: "This action failed an integrity check. Nothing ran.",
	unverifiable: "This action can no longer be verified on this site. Nothing ran.",
	otherTab:
		"Couldn't confirm. It may have been handled in another tab. Refresh, or ask me to try again.",
	expired: "This confirmation expired. Tell me the action again to retry it.",
	safety: SAFETY_TEXT,
	own: "Something the server says in its own words.",
};
const goes = (extra = {}) => ({
	fate: "gone",
	message: "",
	toast: false,
	reload: true,
	cardError: null,
	queued: null,
	safety: null,
	...extra,
});
const spent = (message, extra = {}) => goes({ message, toast: true, reload: false, ...extra });
const safe = (message) => goes({ reload: false, safety: { message } });
const keeps = (message) => ({
	fate: "kept",
	message,
	toast: true,
	reload: false,
	cardError: "$message",
	queued: null,
	safety: null,
});
const storage = (message) => ({ ...keeps(message), cardError: "$error" });

export const EXPECTED = {
	ok: goes(),
	"ok, the continuation turn queued (a draft that continues)": goes({
		queued: { run_id: "run-1", message_id: "msg-1", position: 2 },
	}),
	"ok, queued with no position": goes({
		queued: { run_id: "run-2", message_id: "msg-2", position: null },
	}),
	"no reply at all": goes(),
	"failed after the card was spent (the tool refused)": goes(),
	"partly applied": goes(),
	"the execute call crashed (no reason_code)": goes(),
	"safety update (a pending action)": safe(T.safety),
	"safety update (a revoked legacy token)": safe(T.safety),
	"safety update with no message": safe(T.safety),
	"storage unavailable (confirm_tool)": storage(STORAGE_UNAVAILABLE.error.message),
	"storage outcome unknown (confirm_tool)": storage(STORAGE_UNKNOWN.error.message),
	"storage unavailable (approve_and_run adds a reason_code)": storage(
		STORAGE_UNAVAILABLE.error.message
	),
	"storage outcome unknown (approve_and_run adds a reason_code)": storage(
		STORAGE_UNKNOWN.error.message
	),
	"storage unavailable with no message": storage(undefined),
	"busy (the row is locked)": keeps(T.busy),
	"busy (a guarded structure form is locked)": keeps(T.busy),
	"already executing": keeps(T.executing),
	"identity refused": keeps(T.identity),
	"an armed macro run is stopping": keeps(T.armed),
	"approve and run offered no more (not_runnable)": keeps(T.notRunnable),
	"approve and run: the skill is not armed": keeps(T.notArmed),
	"approve and run: the step needs its own confirmation": keeps(T.ownConfirm),
	"a refusal code the client does not know": keeps(T.own),
	"not found (the token is gone)": spent(T.notFound),
	"already handled elsewhere (confirmed)": spent(T.handled),
	"already handled elsewhere (discarded)": spent(T.handled),
	"stale: the record changed after it was proposed": {
		...spent(T.stale),
		draft: spent(T.draftStale),
	},
	"target missing: the record is gone": spent(T.missing),
	tampered: spent(T.tampered),
	unverifiable: spent(T.unverifiable),
	"a used legacy token, the card has no expiry": spent(T.otherTab),
	"a used legacy token, the card's expiry has passed": spent(T.expired),
	"a used legacy token, the card's expiry is ahead": spent(T.otherTab),
	"an armed run withdrew the confirmation (no reason_code)": spent(T.otherTab),
	"a body that says ok:true but names a failure code": goes(),

	discarded: goes(),
	"already handled (discard of a spent card)": goes(),
	"no reply at all (discard)": goes(),
	"discard refused: busy": keeps(T.busy),
	"discard refused: already executing": keeps(T.executing),
	"discard: storage unavailable": storage(STORAGE_UNAVAILABLE.error.message),
	"discard: storage outcome unknown": storage(STORAGE_UNKNOWN.error.message),
	"discard: safety update": safe(T.safety),
	"discard: ok:true that carries the safety code": safe(T.safety),
	"discard: a refusal with no reason_code": goes(),
	"discard: not found (raw)": goes(),
	"discard: a refusal code the client does not know": keeps(T.own),

	"thrown: a plain error": { thrown: { internal: true } },
	"thrown: the server said why (417)": { thrown: { message: "Due Date is required." } },
	"thrown: the server said why but it was a 500": { thrown: { internal: true } },
	"thrown: offline": {
		thrown: {
			message:
				"Jarvis could not reach the server. Check your internet connection and try again.",
		},
	},
	"thrown: the session expired": {
		thrown: { message: "Your session has expired. Please sign in again." },
	},
	// no session cookie in the test page, so a 403 reads as an expired session
	"thrown: a refused permission on a live session": {
		thrown: { message: "Your session has expired. Please sign in again." },
	},
};
// The door's fallback for a thrown call that carries nothing a person can read.
export const THROWN_FALLBACK = {
	confirm: "Could not confirm this action.",
	approve: "Could not start the run.",
	dismiss: "Could not discard this action.",
};
