// Approval detail client (DESIGN-V3 §8.3, D39) - get_approval returns the full
// row (name, title, status, document_type, conversation, question, context_md,
// options, ref_doctype, ref_name, decision, decided_by, decided_by_name,
// decided_at, creation, owner, can_act) gated like decide(): SM or the owner
// of the linked conversation. Frozen signature; built against B7's endpoint.
import { call } from "frappe-ui";

export const getApproval = (name) => call("jarvis.chat.approvals_api.get_approval", { name });

// Held File Box writes (Jarvis Pending Action, kind file_box_held): the board's
// lane. Owner or System Manager only (server-enforced); card/summary are
// model-derived text and must render as text, never HTML. Every decide response
// carries a `reason_code` (copy lives in lib/heldActions).
export const listPendingActionsLane = () =>
	call("jarvis.chat.approvals_api.list_pending_actions_lane", {});
export const getPendingAction = (name) =>
	call("jarvis.chat.approvals_api.get_pending_action", { name });
export const decideHeldAction = (name, action, useExisting) =>
	call("jarvis.chat.approvals_api.decide_held_action", {
		name,
		action,
		...(useExisting ? { use_existing: useExisting } : {}),
	});
