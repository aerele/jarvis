// The Jarvis Admin's view of every user's skills (the Admin tab of the Skills
// page). `src/api.js` is frozen: new endpoints get thin wrappers in per-feature
// modules under src/api/.
//
// The server gates each of these on the Jarvis Admin role and accepts POST only.
// The client's own gate hides the tab; it is not what protects the data.
import { call } from "frappe-ui";

const SA = "jarvis.chat.skills_admin_api.";

// One page of every non-learned skill, with no instructions.
// `filters`: { owner, audience ("User" | "Role" | "Org"), enabled, armed, on_hold };
// an empty value is "not filtering". The tab says `audience`; the server's filter is
// named `scope`, so it is renamed here and nowhere else. `page` counts from 1 and
// holds `pageSize` rows (the server's cap is 100).
// -> { rows, page, page_size, has_more, total, hold_available, delete_available,
//      handover_available, sync }
// Each row: name, skill_name, description, owner, owner_name, scope, target_role,
// enabled, allow_approve_run, admin_hold, admin_hold_reason, shared_with_count,
// modified, is_mine, is_published, is_learned. `sync`: { pending, needs_apply,
// last_synced_at }. `hold_available`, `delete_available` and `handover_available`
// are false on a site not yet migrated for them.
export const adminListSkills = ({ search = "", filters = {}, page = 1, pageSize = 20 } = {}) => {
	const { audience, ...rest } = filters || {};
	const sent = audience ? { ...rest, scope: audience } : rest;
	return call(SA + "admin_list_skills", {
		filters: JSON.stringify(sent),
		search,
		page,
		page_size: pageSize,
	});
};

// The owner filter's choices, matching `search` (an address or a name).
// -> { owners: [{ user, full_name }], more }
export const adminSkillOwners = (search = "") => call(SA + "admin_skill_owners", { search });

// One skill to look at, read-only: the row fields plus its instructions, who it is
// shared with, its allowed roles, its lineage, the admin history and how many
// macros use it.
// -> { ...row, instructions, shared_with, allowed_roles, lineage, history,
//      used_by_macros: { total, armed } }
export const adminGetSkill = (name) => call(SA + "admin_get_skill", { name });

// Put another user's skill on hold: the assistant stops using it until an admin
// releases it. `reason` is required and is shown to the owner on their skill.
// -> { ok, needs_apply, used_by_macros }
export const adminHold = (skill, reason) => call(SA + "admin_hold", { skill, reason });

// Lift the hold. `restore` also switches a role or company skill back on; a
// private skill is switched on by its owner. Nothing is ever re-armed.
// -> { ok, restored, needs_apply }
export const adminRelease = (skill, restore = false) =>
	call(SA + "admin_release", { skill, restore: restore ? 1 : 0 });

// Delete any user's skill. The owner sees a notice that it happened.
// -> { ok, needs_apply, used_by_macros }
export const adminDelete = (skill) => call(SA + "admin_delete", { skill });

// Who `skill` can be handed to, matching `search` (an address or a name): enabled
// users with the Jarvis User role, not its owner (the server leaves the owner of
// `skill` out). `at_cap` is a user who already owns `cap` skills, the most one
// person may. -> { targets: [{ user, full_name, at_cap }], more, cap }
export const adminHandoverTargets = (search = "", skill = "") =>
	call(SA + "admin_handover_targets", skill ? { search, skill } : { search });

// Move any user's skill to another owner. `expected_owner` is the owner the admin
// saw: the server refuses a skill that changed hands since. It arrives switched
// off, with its shares, File Box opt-in and assignments cleared, and any hold lifted.
// -> { ok, new_owner, cleared: { shares, armed, file_box, assignments },
//      hold_lifted, requests_cancelled, needs_apply }
export const adminHandover = (skill, new_owner, expected_owner) =>
	call(SA + "admin_handover", { skill, new_owner, expected_owner });
