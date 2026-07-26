// Skills-page API additions (DESIGN-V3 §8.4). `src/api.js` is frozen - new
// endpoints get thin wrappers in per-feature modules under src/api/.
import { call } from "frappe-ui";

// §8.3 - bulk delete of own skills. The server skips shared/not-owned rows
// with per-row reasons and enqueues ONE skills-apply at the end.
// -> { deleted: int, skipped: [{name, reason}] }
export const deleteCustomSkillsBulk = (names) =>
	call("jarvis.chat.custom_skills_api.delete_custom_skills_bulk", {
		names: JSON.stringify(Array.from(names || [])),
	});

// ── skill promotion (requester side, Skills-area promotion surfacing) ─────────
const CS = "jarvis.chat.custom_skills_api.";

// Ask a reviewer to widen one of MY OWN User-scope skills up the ladder
// (User → Role/Org). Files a Pending request and pings reviewers; the skill is
// untouched until a reviewer decides. -> { ok, request, skill }
export const requestSkillPromotion = ({ name, to_scope, target_role = "", note = "" }) =>
	call(CS + "request_skill_promotion", { name, to_scope, target_role, note });

// My most-recent promotion request for one of my skills, for the status chip.
// Owner-scoped server-side. -> {} | {name, status, from_scope, to_scope,
// target_role, note, reviewer, reviewer_name, decided_at, decision_note, created}
export const mySkillPromotion = (name) => call(CS + "my_skill_promotion", { name });

// Role options for the promotion Role picker: MY OWN targetable desk roles
// (a requester promotes to a team they belong to). -> { roles: [...] }
export const promotableTargetRoles = () => call(CS + "promotable_target_roles");
