// Org-promotion push-budget warning (Skills-area promotion surfacing, Fable
// ruling 2). Extracted as a PURE, importable, node-tested module (the
// eventFence.js precedent) so the boundary logic is verified without a browser.
//
// Only Org promotions reach the shared container push; Role and User promotions
// never do (build_push_payload pushes ONLY scope=Org rows with no allowed_roles),
// so a non-Org target never warns. `pushCount` is the CURRENT count of pushable
// Org skills (the exact build_push_payload eligibility filter, UNCAPPED, from the
// reviewer-gated list endpoint); `pushBudget` is the real MAX_SKILLS_PER_PUSH
// constant. Approving an Org promotion adds exactly one pushable skill.
//
// Levels (the approval is ALWAYS allowed — the reviewer decides informed):
//   "over" — after approval the pushable count EXCEEDS the budget: the newly
//            promoted skill is truncated out of the next container push until an
//            Org skill is disabled/removed (loud, red).
//   "near" — after approval the count EQUALS the budget: the last free push slot
//            is taken (amber) — warned early so the NEXT approval isn't a surprise.
//   null   — room to spare, or a non-Org target.
export function orgPushBudgetWarning({ toScope, pushCount, pushBudget } = {}) {
	if (toScope !== "Org") return null;
	const budget = Number(pushBudget) || 0;
	if (budget <= 0) return null;
	const count = Math.max(Number(pushCount) || 0, 0);
	const afterApprove = count + 1;
	if (afterApprove > budget) {
		return {
			level: "over",
			count,
			budget,
			afterApprove,
			message:
				`The shared container already holds ${count} of ${budget} pushable Org skills. ` +
				`Approving makes ${afterApprove} — over the ${budget}-skill push budget, so this ` +
				`newly promoted skill will NOT reach the container until another Org skill is ` +
				`disabled or removed. You can still approve; the promotion takes effect immediately, ` +
				`only the container push is capped.`,
		};
	}
	if (afterApprove === budget) {
		return {
			level: "near",
			count,
			budget,
			afterApprove,
			message:
				`Approving fills the shared container to ${afterApprove} of ${budget} pushable Org ` +
				`skills — the last free slot in the push budget.`,
		};
	}
	return null;
}
