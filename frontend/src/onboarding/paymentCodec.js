/**
 * Turn one HTTP answer from the bench facade into a machine-readable verdict.
 *
 * Pure, so `node --test` runs it with no bundler or DOM. The wizard keys on
 * `error.code` / `contract_code` and NEVER on HTTP status prose - every branch
 * below exists to get a stable code out of a response whatever shape the
 * response takes, and there are three shapes because the contract genuinely has
 * three:
 *
 *   1. **facade success** - `{ok:true, contract_version, data, context}` under a
 *      200. `data` is admin's envelope passed through (its own `code`, its
 *      capability flags, its billing disclosure), minus the credential-shaped
 *      keys the facade strips on the way out.
 *   2. **facade failure** - `{ok:false, error, context}` under a DELIBERATE
 *      4xx/5xx (onboarding_contract.failure). Never `{ok:false}` on a 200, so
 *      the status is a real signal to the transport layer while the CODE is what
 *      this decoder reads.
 *   3. **the OLD-shape throw** - `finish_payment` still raises a bare
 *      `frappe.throw`, and onboarding_contract.stamp_error parks the
 *      machine-readable error on `frappe.local.response["error"]` next to
 *      Frappe's own `exc_type`. So a THROWN failure carries a generic status
 *      (417/500/…) that means nothing, plus an `error` object at the TOP level
 *      of the body - not nested under `message`, and not in an `{ok:false}`
 *      envelope. The reducer must read that different shape.
 *
 * The input is transport-shaped, not frappe-ui-shaped, on purpose: the caller
 * hands over `{status, body, networkError}` (see usePaymentFlow's rawCall), so
 * this module is testable against every wire shape without a fetch stub and
 * without frappe-ui's own error massaging in the way.
 */

import { CODES } from "./paymentCodes.js";

/** The network never answered. Says nothing about the payment. */
export const CLIENT_OFFLINE = "CLIENT_OFFLINE";
/** An answer we could not read a code out of. Honestly unknown - never a decline. */
export const CLIENT_UNREADABLE = "CLIENT_UNREADABLE";

// The one exception class stable all the way back to the v2 fork: every admin
// throws frappe.DuplicateEntryError for a duplicate signup, and its 409 rides
// the wire even on a control plane far older than the machine contract. It is
// therefore the only throw we may key structurally when no stamped code is
// present - see onboarding_contract.is_duplicate_signup, the bench-side twin.
const EXC_TYPE_TO_CODE = {
	DuplicateEntryError: CODES.ACCOUNT_ALREADY_EXISTS,
};

function parseServerMessage(body) {
	// Frappe wraps a throw's sentence in _server_messages as a JSON array of
	// JSON strings. Read it only as a last-resort human message - NEVER to branch
	// on, which is the exact prose-matching this whole contract removed.
	const raw = body && body._server_messages;
	if (!raw) return "";
	try {
		const arr = JSON.parse(raw);
		const first = Array.isArray(arr) ? arr[0] : arr;
		const parsed = typeof first === "string" ? JSON.parse(first) : first;
		return (parsed && parsed.message) || "";
	} catch (e) {
		return "";
	}
}

function verdict(over) {
	return {
		ok: false,
		code: CLIENT_UNREADABLE,
		message: "",
		recovery: "",
		retryAfterSeconds: 0,
		excType: "",
		contractVersion: null,
		data: {},
		context: {},
		error: {},
		httpStatus: 0,
		...over,
	};
}

/**
 * @param {{status?: number, body?: object|null, networkError?: boolean}} res
 */
export function decode(res) {
	res = res || {};
	const status = Number(res.status) || 0;

	if (res.networkError || status === 0) {
		return verdict({
			code: CLIENT_OFFLINE,
			message: "We could not reach the payment service.",
			httpStatus: status,
		});
	}

	const body = res.body;
	if (!body || typeof body !== "object") {
		return verdict({ code: CLIENT_UNREADABLE, httpStatus: status });
	}

	// ---- shape 1 & 2: the facade envelope, nested under Frappe's `message` ----
	const envelope = body.message;
	if (envelope && typeof envelope === "object" && typeof envelope.ok === "boolean") {
		if (envelope.ok) {
			const data =
				(envelope.data && typeof envelope.data === "object" && envelope.data) || {};
			return {
				ok: true,
				code: data.code || "",
				message: "",
				recovery: data.recovery || "",
				retryAfterSeconds: 0,
				excType: "",
				contractVersion: envelope.contract_version ?? null,
				data,
				context:
					(envelope.context &&
						typeof envelope.context === "object" &&
						envelope.context) ||
					{},
				error: {},
				httpStatus: status,
			};
		}
		const error =
			(envelope.error && typeof envelope.error === "object" && envelope.error) || {};
		return verdict({
			code: error.code || CLIENT_UNREADABLE,
			message: error.message || "",
			recovery: error.recovery || "",
			retryAfterSeconds: Number(error.retry_after_seconds) || 0,
			contractVersion: envelope.contract_version ?? null,
			context:
				(envelope.context && typeof envelope.context === "object" && envelope.context) ||
				{},
			error,
			httpStatus: status,
		});
	}

	// ---- shape 1 (flat): a pre-contract success (start_signup / finish_payment)
	// answers a bare dict under `message`, with no ok/data envelope. Treat it as
	// data; effectiveCode maps its fields onto the vocabulary.
	if (envelope && typeof envelope === "object") {
		return {
			ok: status < 400,
			code: envelope.code || "",
			message: "",
			recovery: envelope.recovery || "",
			retryAfterSeconds: 0,
			excType: "",
			contractVersion: envelope.contract_version ?? null,
			data: envelope,
			context: {},
			error: {},
			httpStatus: status,
		};
	}

	// ---- shape 3: the OLD-shape throw. The stamped error sits at the TOP level
	// of the error body, beside Frappe's exc_type - not under `message`.
	const excType = body.exc_type || "";
	const stamped = body.error && typeof body.error === "object" ? body.error : null;
	const serverMessage = parseServerMessage(body);
	if (stamped && stamped.code) {
		return verdict({
			code: stamped.code,
			message: stamped.message || serverMessage,
			recovery: stamped.recovery || "",
			retryAfterSeconds: Number(stamped.retry_after_seconds) || 0,
			excType,
			error: stamped,
			httpStatus: status,
		});
	}

	// An unstamped throw: an admin older than the stamping build, or a Frappe
	// framework error. Fall back to (status, exc_type) - the duplicate is the one
	// class we may read - and NEVER to the sentence.
	const mapped = EXC_TYPE_TO_CODE[excType];
	return verdict({
		code: mapped || CLIENT_UNREADABLE,
		message: serverMessage,
		excType,
		httpStatus: status,
	});
}

/**
 * The code a decoded SUCCESS actually means.
 *
 * A coded envelope is taken at its word. A FLAT legacy success (a pre-contract
 * start_signup, or finish_payment, neither of which returns the contract
 * envelope) carries no code, so its fields are mapped onto the vocabulary
 * STRUCTURALLY - handles ⇒ a live intent, pending_verification ⇒ verification,
 * Active or a connection payload ⇒ paid - never by reading a message. This
 * replaces the retired steps.verifyPollAction, which was the last place two
 * different readings of these shapes could drift apart. Anything with nothing to
 * go on is honestly unknown rather than guessed.
 */
export function effectiveCode(decoded) {
	if (!decoded) return CLIENT_UNREADABLE;
	if (decoded.code) return decoded.code;
	const d = decoded.data || {};
	if (d.pending_verification) return CODES.SIGNUP_VERIFICATION_REQUIRED;
	const hasHandles =
		d.razorpay_order_id ||
		d.razorpay_subscription_id ||
		d.payment_session_id ||
		d.cashfree_order_id ||
		d.subscription_session_id ||
		d.cashfree_subscription_id;
	if (hasHandles) return CODES.PAYMENT_CONFIRMATION_PENDING;
	if (d.subscription_status === "Active") return CODES.PAYMENT_ALREADY_ACTIVE;
	// finish_payment's success is FLAT and carries no code: what it returns is a
	// CONNECTION payload, which is admin's way of saying the money is confirmed.
	// Both of its success shapes are paid - the allocated one
	// (`_connection_payload`) and the allocation-FAILURE one (`_pending_payload`,
	// which records the payment and pages ops) - so the discriminator is the
	// presence of the tenant/connection keys, NOT `agent_url` being truthy
	// (_pending_payload sets agent_url to ""). This is what stops a bare 200 with
	// an empty or unrelated body from being read as a completed payment.
	if ("tenant_status" in d || "agent_url" in d) return CODES.PAYMENT_ALREADY_ACTIVE;
	return CLIENT_UNREADABLE;
}
