// The "Approve & run" arming toggle's gating + disclosure logic (skill
// approve-and-run, P2), extracted as a PURE, importable, node-tested module (the
// promotionBudget.js precedent) so the security-adjacent boolean matrix and the
// admin-facing risk copy are verified without a browser. SkillDetail.vue's
// computeds call these; the SERVER guard (_guard_allow_approve_run_enable) stays
// the sole authority on the actual persisted transition — this only drives the
// switch's disabled state + description.

// The covered writes an armed run applies uncarded once approved. Kept in plain
// language for an admin's arm/don't-arm decision, and DELIBERATELY inclusive of
// "any other whitelisted action" so the disclosure reflects the real covered set
// — which includes run_method (the generic whitelisted-method escape hatch), not
// only the doc-shaped verbs. Undersell here would let an admin arm a standing
// write-confirmation bypass believing the blast radius is ordinary CRUD.
const BASE_DESCRIPTION =
	"When on, a run of this skill with 2 or more steps offers a single " +
	"“Approve & run”: after one approval its create, update, submit, email, " +
	"workflow, share, assign and any other whitelisted action it calls run " +
	"without asking each time (each still leaves a receipt); delete, cancel and " +
	"amend still ask. Turning it off takes effect immediately once saved.";

// The switch is locked (non-interactive) iff the skill is read-only to this
// viewer, OR it is UNARMED and this viewer can't arm it. Enabling (0 -> 1) is
// Jarvis Admin / System Manager only; DISABLING is always free for the owner (the
// kill switch), so an already-armed skill (`savedArmed`) is never locked for an
// editor. Mirrors the doctype's _guard_allow_approve_run_enable exactly.
export function armToggleLocked(readonly, savedArmed, canArm) {
	return !!readonly || (!savedArmed && !canArm);
}

// The switch's description. Appends the "why it's locked" hint ONLY when the lock
// is because an unarmed skill can't be armed by this viewer (an admin, or an
// already-armed skill, needs no explanation).
export function armToggleDescription(savedArmed, canArm) {
	return !savedArmed && !canArm
		? BASE_DESCRIPTION + " Only a Jarvis Admin can arm a skill."
		: BASE_DESCRIPTION;
}
