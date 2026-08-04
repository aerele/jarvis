// Vendored copies of the admin LLM-apply-operation contract shapes, taken VERBATIM
// from the landed admin half (jarvis_admin_v2 @ feat/plan05-apply-operation,
// fleet/apply_operation.py::status + api/tenant.py). These are the exact shapes the
// bench receives, so the client seam in llmOperation.js is exercised against them
// and the pair is diffed at integration.
//
// FROZEN CONTRACT (jarvis_admin_v2.fleet.apply_operation):
//
//   save_llm_pool(...) returns result["apply_operation"];
//   get_llm_apply_operation(operation_id) returns .data — the SAME 12-key shape.
//   Even while pending, every key is present:
//
//     { operation_id, state, code, message, tenant (OPAQUE 32-hex, never TEN-*),
//       tenant_authority_generation, desired_version, applied_version,
//       chat_readiness, chat_readiness_reason, retryable, retry_after_seconds }
//
//   state  ∈ { pending, applying, applied_waiting_readiness, ready, failed, superseded }
//   code   ∈ { LLM_APPLY_PENDING, LLM_APPLYING, LLM_APPLIED_READINESS_PENDING,
//              LLM_READY, LLM_APPLY_REJECTED (permanent), LLM_APPLY_FAILED (transient),
//              LLM_APPLY_BLOCKED_OAUTH, SUPERSEDED_NEWER_SAVE, SUPERSEDED_AUTHORITY_CHANGED }
//   retryable = (code !== "LLM_APPLY_REJECTED")   [admin _retryable]
//
//   Wire refusals (thrown, not operation states):
//     CUSTOMER_NOT_PAID (403), TENANT_AUTHORITY_REPAIR_REQUIRED (safe envelope),
//     IdempotencyKeyConflict (409), RateLimitExceeded (429 + retry_after_seconds),
//     UnknownOperation (404).

// A 32-hex opaque tenant handle (admin: sha256(...)[:32]) — never a raw TEN-* name.
const TENANT = "7f3a9c1e4b2d8a6f0e5c3b1a9d7e2f40";
const GEN = 7;
const OP_ID = "llmapply_a1b2c3d4e5";

/**
 * Build a status/descriptor in the exact 12-key shape.
 * @param {string} state
 * @param {object} over - per-field overrides
 */
export function statusOf(state, over = {}) {
	const base = {
		operation_id: OP_ID,
		state,
		code: "",
		message: "",
		tenant: TENANT,
		tenant_authority_generation: GEN,
		desired_version: 12,
		applied_version: 0,
		chat_readiness: null,
		chat_readiness_reason: "",
		retryable: true,
		retry_after_seconds: 0,
	};
	const byState = {
		pending: {
			code: "LLM_APPLY_PENDING",
			message: "Your AI connection is saved. Starting setup.",
			chat_readiness: "Provisioning",
		},
		applying: {
			code: "LLM_APPLYING",
			message: "Applying your AI connection.",
			chat_readiness: "Configuring",
		},
		applied_waiting_readiness: {
			code: "LLM_APPLIED_READINESS_PENDING",
			message: "Your AI connection is saved. Jarvis is finishing setup.",
			applied_version: 12,
			chat_readiness: "Configuring",
			chat_readiness_reason: "Applying the ERP plugin environment.",
		},
		ready: {
			code: "LLM_READY",
			message: "Ready.",
			applied_version: 12,
			chat_readiness: "Ready",
			retryable: false, // _retryable is code!==REJECTED, but ready is terminal-success
		},
		failed: {
			// Transient failure — the admin projects this as RETRYABLE.
			code: "LLM_APPLY_FAILED",
			message: "We couldn't apply your AI connection.",
			chat_readiness: "Provisioning",
			retryable: true,
		},
		rejected: {
			// Permanent — the request itself is invalid (bad key / rejected spec).
			state: "failed",
			code: "LLM_APPLY_REJECTED",
			message: "That AI configuration was rejected. Check the key and try again.",
			retryable: false,
		},
		blocked_oauth: {
			state: "failed",
			code: "LLM_APPLY_BLOCKED_OAUTH",
			message:
				"We couldn't finish connecting your subscription. Please reconnect the account.",
			retryable: true,
		},
		superseded: {
			code: "SUPERSEDED_NEWER_SAVE",
			message: "A newer AI connection change replaced this one.",
		},
		superseded_authority: {
			state: "superseded",
			code: "SUPERSEDED_AUTHORITY_CHANGED",
			message: "Your workspace assignment changed; please retry from the current setup.",
		},
	};
	return { ...base, ...(byState[state] || {}), ...over };
}

/** What save_llm_pool returns the instant the desired intent is persisted. */
export const saveDescriptor = statusOf("pending", { operation_id: OP_ID });

/** A RateLimitExceeded refusal on save (429): thrown, carries retry_after_seconds. */
export function rateLimitRefusal(retryAfterSeconds = 30) {
	return {
		code: "RateLimitExceeded",
		status: 429,
		retry_after_seconds: retryAfterSeconds,
		message: "Too many changes. Try again shortly.",
	};
}

export const OP_IDS = { OP_ID, TENANT, GEN };
