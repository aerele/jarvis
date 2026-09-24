// Approval detail client (DESIGN-V3 §8.3, D39) - get_approval returns the full
// row (name, title, status, document_type, conversation, question, context_md,
// options, ref_doctype, ref_name, decision, decided_by, decided_by_name,
// decided_at, creation, owner, can_act) gated like decide(): SM or the owner
// of the linked conversation. Frozen signature; built against B7's endpoint.
import { call } from "frappe-ui";

export const getApproval = (name) => call("jarvis.chat.approvals_api.get_approval", { name });

// The board's lane of Jarvis Pending Actions: held File Box writes (owner or System
// Manager) and the viewer's own chat cards (owner only; decided through the chat's
// confirmTool / dismissTool). All server-enforced; card/summary/conversation_title
// are model-derived text and must render as text, never HTML. Every decide response
// carries a `reason_code` (copy lives in lib/heldActions and lib/chatCardActions).
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
// Edit & create (PR-2d): `values` = one patch per record over the proposed values
// (only what the approver changed). Answers like decideHeldAction, plus `errors`
// ([{doc_index, fieldname, parentfield?, idx?, label, message}]) on a refusal.
export const editAndCreateHeld = (name, values) =>
	call("jarvis.chat.approvals_api.edit_and_create_held", { name, values });
// A File Box approval sheet (kind file_box_sheet; its detail comes from
// getPendingAction): the lazy Use existing candidates, the one apply and the
// "Skip this file". Apply and discard answer {ok, reason_code, ...}; a refusal
// may carry `errors` ({index | question | "sheet": message}) and `answer_errors`.
export const getSheetCandidates = (name, indexes) =>
	call("jarvis.chat.approvals_api.get_sheet_candidates", { name, indexes });
// The slim poll while a sheet lists or applies: status/collecting/progress/sha
// only (held_sheet_board.status) - no records, no unseal.
export const getSheetStatus = (name) =>
	call("jarvis.chat.approvals_api.get_pending_action", { name, slim: 1 });
export const applySheet = (name, decisions) =>
	call("jarvis.chat.approvals_api.apply_sheet", { name, decisions });
export const discardSheet = (name) => call("jarvis.chat.approvals_api.discard_sheet", { name });
