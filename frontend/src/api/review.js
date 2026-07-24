// Review-tab API (Skills-area rework, DESIGN.md §6b) - thin wrappers around the
// reviewer-set endpoints in `jarvis.chat.learned_api.*` that are NEW with the
// rework: the wiki-promotion queue, the server-assembled "Go to chat" bundle and
// the reviewer follow-up-question trigger. Kept in their own module (not
// src/api/learning.js, which F1 owns) so the two waves never touch the same
// file. Mirrors src/api/learning.js: one `call` per endpoint, flat kwargs.
//
// `getReviewAccess` is re-exported from ./learning (F1 defines it there) so
// ReviewTab imports its probe and its promotion/follow-up bindings from a single
// module - the same re-export idiom src/api/voice.js uses for `dismissWikiNudge`.
import { call } from "frappe-ui";

const LR = "jarvis.chat.learned_api.";
// Skill-promotion decide/list live in custom_skills_api (the skill CRUD module),
// NOT learned_api — the skill queue is a sibling of the wiki queue, not a fork.
const CS = "jarvis.chat.custom_skills_api.";

// Reviewer-set + self-host-aware access probe: {self_hosted, pending_promotions,
// pending_patterns}. Defined in F1's learning.js; surfaced here so the Review tab
// has one import site for every reviewer-only binding.
export { getReviewAccess } from "./learning";

// ── wiki-promotion queue (DESIGN.md 2.4 / 6b) ────────────────────────────────
// Paginated `Jarvis Wiki Promotion Request` list, envelope parity with
// listLearnedPatternsPage ({rows, total, has_more, start, page_length}). Rows:
// {name, page, page_title, from_scope, to_scope, target_role, requested_by,
//  requested_by_name, note, body_excerpt, status, created}. `status` "Pending"
// (default) | "Approved" | "Rejected" | "All".
export const listPromotionRequestsPage = (p = {}) =>
	call(LR + "list_promotion_requests_page", {
		status: p.status || "Pending",
		search: p.search || "",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Approve (approve truthy) or reject (falsy) a promotion request. On approve the
// server merges the frozen body_snapshot into the Role/Org target page (the
// source User page stays intact); a `note` is stored as the decision note.
// Returns {ok, status} on success or {ok:false, reason} for a stale/non-Pending
// request. approve is coerced to 1/0 for the server's cint().
export const decidePromotion = (name, approve, note = "") =>
	call(LR + "decide_promotion", { name, approve: approve ? 1 : 0, note });

// Server-assembled background bundle the reviewer carries into a fresh chat via
// chatPrefill (richer than the client buildDiscussPrompt: origin, the linked
// question + the user's answer, who the user is + roles, the approval
// implication, a unified diff). `kind` in {"pattern", "promotion"}; `name` is the
// Jarvis Learned Pattern or Jarvis Wiki Promotion Request. Returns {prompt}.
export const goToChatContext = (kind, name) => call(LR + "go_to_chat_context", { kind, name });

// Rephrase a reviewer's ask into ONE generic-tone Personalise question and insert
// it into the target user's bank (origin "From your organisation" - the user
// never sees reviewer attribution). `name` is the Jarvis Learned Pattern (target
// = its linked question's user, else the evidence owner) OR the Jarvis Wiki
// Promotion Request (target = the requester). Returns {ok, name, question}.
export const triggerFollowupQuestion = (name, ask) =>
	call(LR + "trigger_followup_question", { name, ask });

// ── skill-promotion queue (Skills-area promotion surfacing) ──────────────────
// The reviewer queue for `Jarvis Skill Promotion Request`, the sibling of the
// wiki promotions queue. Envelope parity ({rows, total, has_more, start,
// page_length}) PLUS coarse push-budget context {push_count, push_budget}. Rows:
// {name, skill, skill_name, from_scope, to_scope, target_role, note, status,
// requested_by, requested_by_name, created, reviewer, decided_at, decision_note,
// body_excerpt, description_snapshot, user_invocable_snapshot, push_projection}.
// `body_excerpt`/`description_snapshot`/`user_invocable_snapshot` are the FULL
// immutable content approval publishes for Pending rows (not truncated live
// excerpts — R2-SP-2 surfaces EVERY field materialization consumes); empty on a
// decided row. `push_projection` is the server's truthful per-row Org push-budget
// projection (render with formatPushProjection, never a client-side guess).
export const listSkillPromotions = (p = {}) =>
	call(CS + "list_skill_promotion_requests", {
		status: p.status || "Pending",
		search: p.search || "",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Approve (truthy) or reject (falsy) a skill promotion. On approve the server
// PUBLISHES a new system-owned Role/Org skill from the request's immutable content
// snapshot, leaving the requester's private skill intact (four-eyes: a reviewer
// cannot approve their OWN request → PermissionError; TOCTOU-safe). `ackProjection`
// is the fresh Org push-budget projection the reviewer just confirmed against
// (R2-SP-5): the server recomputes it under the decision and, if a warning-worthy
// catalog moved since, returns {ok:false, needs_reconfirm:true, push_projection}
// WITHOUT publishing so the reviewer reconfirms the new impact. Returns {ok,
// status, skill, materialized, push_projection?} on success, or {ok:false, reason,
// needs_reconfirm?, push_projection?} for a stale/changed/already-decided request.
export const decideSkillPromotion = (name, approve, note = "", ackProjection = null) =>
	call(CS + "decide_skill_promotion", {
		request_name: name,
		approve: approve ? 1 : 0,
		note,
		ack_projection: ackProjection ? JSON.stringify(ackProjection) : "",
	});

// Fresh push-budget projection for one Pending promotion, recomputed at the moment
// the reviewer is about to decide (CDX-SP-2) — a list-load value goes stale under
// concurrent promotions/edits. Returns {ok, to_scope, push_projection}; render
// push_projection with formatPushProjection (never a client-side guess).
export const preflightSkillPromotion = (name) =>
	call(CS + "preflight_skill_promotion", { request_name: name });
