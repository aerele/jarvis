// Docmeta client (DESIGN-V3 §8.1 + §14 F1) - thin call() wrappers around the
// jarvis.chat.docmeta_api endpoints (B7). Signatures are FROZEN by the design;
// the SPA is built against them. All endpoints are gated server-side
// (owner / conversation-owner / SM; assignees get DocShare read).
import { call } from "frappe-ui";

const DM = "jarvis.chat.docmeta_api.";

// Single round-trip bundle: {comments, assignees, liked_by, liked, shares,
// attachments, created, modified} (D22 + §14 F1 `shares`).
export const getDocmeta = (doctype, name) => call(DM + "get_docmeta", { doctype, name });

// Comments - add returns the new row (get_docmeta shape); update returns the
// updated row; delete returns nothing.
export const addComment = (doctype, name, content) =>
	call(DM + "add_comment", { doctype, name, content });
export const updateComment = (comment, content) =>
	call(DM + "update_comment", { comment, content });
export const deleteComment = (comment) => call(DM + "delete_comment", { comment });

// Assignees - returns the fresh assignees list. action: "add" | "remove".
// Not offered on Jarvis Custom Skill (§14 DA-09 - server allowlist matches).
export const toggleAssignment = (doctype, name, user, action = "add") =>
	call(DM + "toggle_assignment", { doctype, name, user, action });

// Shares (§14 F1) - DocShare read grant for Macro/Approval/Agent Installation
// (skills keep their child-table share model). action: "add" | "remove".
export const toggleShare = (doctype, name, user, action = "add") =>
	call(DM + "toggle_share", { doctype, name, user, action });

// Like - hides the desk `add="Yes"` string quirk (D25); returns liked_by list.
export const toggleLike = (doctype, name, like = 1) =>
	call(DM + "toggle_like", { doctype, name, like: like ? 1 : 0 });

// Attachment delete (upload goes through stock /api/method/upload_file via
// frappe-ui FileUploader with {doctype, docname} upload-args).
export const deleteAttachment = (doctype, name, file) =>
	call(DM + "delete_attachment", { doctype, name, file });
