// Scope-ladder rank shared by every promotion UI surface (#595 code review).
// SkillDetail.vue's "is there anything wider left to promote to?" gate and
// PromotionRequestDialog's target picker both compare scopes on the SAME
// ladder the server uses (custom_skills_api._SCOPE_RANK) - duplicating the map
// in both files let them silently drift apart.
export const SCOPE_RANK = { User: 0, Role: 1, Org: 2 };

// True when `candidate` sits strictly wider than `baseline` on the ladder. An
// unrecognised baseline (unset, "", a future scope the client doesn't know yet)
// ranks below every real scope, so "nothing known yet" never falsely hides a
// real target - matching the server's own permissive-until-proven-narrower
// defaulting in `_effective_shared_scope`.
export function isWiderScope(candidate, baseline) {
	return (SCOPE_RANK[candidate] ?? -1) > (SCOPE_RANK[baseline] ?? -1);
}
