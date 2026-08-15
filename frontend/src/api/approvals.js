// Approval detail client (DESIGN-V3 §8.3, D39) - get_approval returns the full
// row (name, title, status, document_type, conversation, question, context_md,
// options, ref_doctype, ref_name, decision, decided_by, decided_by_name,
// decided_at, creation, owner, can_act) gated like decide(): SM or the owner
// of the linked conversation. Frozen signature; built against B7's endpoint.
import { call } from "frappe-ui";

export const getApproval = (name) => call("jarvis.chat.approvals_api.get_approval", { name });
