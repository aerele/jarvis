// Learning-board API (plan §6.4) - thin wrappers around
// `jarvis.chat.learned_api.*`, the SM-gated endpoints behind the Skills-page
// "Learning" tab. Mirrors src/api/skills.js: one `call` per endpoint;
// list/settings/batch args are JSON-encoded where the server `_parse_json`s
// them.
import { call } from "frappe-ui";

const LR = "jarvis.chat.learned_api.";

// ── list / board ─────────────────────────────────────────────────────────────
// Flat kwargs (NOT the frozen `filters` JSON envelope the four feature lists
// use): the endpoint takes domain/status/strength/search/surfaced directly.
// `surfaced`: 1 (default review board) | 0 | "all" (decided tabs).
// `view`: "" (default board) | "decided" - the Review tab's Decided log; the
// server then OVERRIDES status with every human-touched terminal/parked state,
// ignores `surfaced` and orders by reviewed_at (nulls last; `sort` "newest"
// (default) | "oldest" flips it). `disposition` filters the decided view only
// (the server refuses it elsewhere): "" | approved | applied | acknowledged |
// rejected | snoozed.
export const listLearnedPatternsPage = (p = {}) =>
	call(LR + "list_learned_patterns_page", {
		domain: p.domain || "",
		status: p.status || "Proposed",
		strength: p.strength || "",
		search: p.search || "",
		surfaced: p.surfaced == null ? 1 : p.surfaced,
		start: p.start || 0,
		page_length: p.page_length || 20,
		view: p.view || "",
		disposition: p.disposition || "",
		sort: p.sort || "",
	});

// Full row + drill-down stats (raw n / confidence / wilson / gap), detected
// roles, compiled-bullet preview, exceptions (SM may see named parties), runs.
export const getLearnedPattern = (name) => call(LR + "get_learned_pattern", { name });

// ── lifecycle transitions (§6.5) - human SM actions, TOCTOU-safe server-side ──
// Proposed→Approved (or Stale→Approved). Passing an edited draft freezes the
// evidence line (draft_edited=1); omit it to approve as-drafted.
export const approveLearnedPattern = (name, editedSkillDraft) =>
	call(
		LR + "approve_learned_pattern",
		editedSkillDraft != null && editedSkillDraft !== ""
			? { name, edited_skill_draft: editedSkillDraft }
			: { name }
	);
export const unapproveLearnedPattern = (name) => call(LR + "unapprove_learned_pattern", { name });
export const rejectLearnedPattern = (name, reason) =>
	call(LR + "reject_learned_pattern", { name, reason });
// B/C insight-only disposition (Phase 1): records that the SM read the insight
// and dismisses it (stored server-side as a terminal Rejected + a stable note).
// A-class rows are refused server-side - they must be Approved to reach the
// container. Wired to the "Acknowledge (insight only)" card action.
export const acknowledgeLearnedPattern = (name) =>
	call(LR + "acknowledge_learned_pattern", { name });
export const restoreRejectedPattern = (name) => call(LR + "restore_rejected_pattern", { name });
export const snoozeLearnedPattern = (name, days) =>
	call(LR + "snooze_learned_pattern", { name, days });
// Correction loop (§6.5): any signed-in desk (System) user, NOT SM-gated,
// flags an Active/Approved learned default as wrong-here. One entry per user
// (a re-flag updates it); ≥2 distinct users or ≥3 events demotes the strength
// band one level and notifies SMs. Guest/portal sessions are refused server-side.
export const flagLearnedDefault = (name, note = "") =>
	call(LR + "flag_learned_default", { name, note });
// A-class only; a mixed batch (any B/C) is refused whole, server-side.
export const batchApprove = (names) =>
	call(LR + "batch_approve", { names: JSON.stringify(Array.from(names || [])) });

// ── insight → skill (wiki-v2 D5) ─────────────────────────────────────────────
// B/C insights never compile into learned skills; "Apply to skill…" folds one
// into an org custom skill instead. draft_* makes ONE LLM call server-side and
// returns a verdict without writing ({worth_applying, reason, action:
// "update"|"create"|"none", skill_name, before_instructions,
// updated_instructions, new_skill}); apply_* performs the confirmed write and
// marks the pattern acknowledged with an applied-to-skill note. The updated
// skill rides the normal Skills-tab apply (no auto-push).
export const draftInsightSkillUpdate = (patternName) =>
	call(LR + "draft_insight_skill_update", { pattern_name: patternName });
export const applyInsightSkillUpdate = (patternName, payload = {}) => {
	const args = { pattern_name: patternName, action: payload.action || "" };
	if (payload.skill_name) args.skill_name = payload.skill_name;
	if (payload.updated_instructions) args.updated_instructions = payload.updated_instructions;
	// dict arg JSON-encoded like batch_approve / setLearningSettings
	if (payload.new_skill) args.new_skill = JSON.stringify(payload.new_skill);
	return call(LR + "apply_insight_skill_update", args);
};

// ── apply / sync (learned skills ride the custom-skill push, §6.2) ───────────
export const applyLearnedSkills = () => call(LR + "apply_learned_skills");
// Proxies get_custom_skills_sync_status - same pill the Skills page polls.
export const getLearnedApplyStatus = () => call(LR + "get_learned_apply_status");
// Board badge: surfaced patterns still awaiting a decision.
export const pendingLearnedCount = () => call(LR + "pending_learned_count");

// ── run now (§5.1 / §5.2 manual bypass) ──────────────────────────────────────
export const runPatternAnalysisNow = () => call(LR + "run_pattern_analysis_now");

// ── settings + status (the in-tab config surface, §6.4) ──────────────────────
export const getLearningSettings = (includePreflight = 0) =>
	call(LR + "get_learning_settings", { include_preflight: includePreflight ? 1 : 0 });
export const setLearningSettings = (payload) =>
	call(LR + "set_learning_settings", { payload: JSON.stringify(payload || {}) });
// SM-only - the status probe the Analysis tab renders from
// (reports {enabled, last/next run, ...}).
export const getLearningStatus = () => call(LR + "get_learning_status");

// ── Review-tab access probe (Skills-area rework, DESIGN.md §6/§6b) ──────────
// Reviewer-set role check ONLY (Jarvis Skill Reviewer | Jarvis Admin | System
// Manager | Administrator). Review visibility is a SEPARATE gate from Analysis
// now (SkillsPage's tab strip uses the collapsed `get_skills_area_caps` probe
// for that; this one is for ReviewTab's own reviewer-only affordances - e.g.
// "Ask the user" follow-ups, promotion decisions). Reports
// {pending_promotions, pending_patterns} so the tab's two queue-type chips
// render their counts without a second round-trip.
export const getReviewAccess = () => call(LR + "get_review_access");
