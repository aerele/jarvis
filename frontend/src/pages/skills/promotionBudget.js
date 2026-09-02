// Org-promotion push-budget warning (Skills-area promotion surfacing, Fable
// ruling 2 + Codex CDX-SP-2). Extracted as a PURE, importable, node-tested module
// (the eventFence.js precedent) so the boundary logic is verified without a
// browser.
//
// This ONLY FORMATS the server's projection — it never re-derives which skill is
// dropped. `custom_skills.project_org_promotion_push` computes the truth on the
// server, sharing `build_push_payload`'s exact eligibility + `skill_name asc`
// ordering + 25-skill cap, and returns it per Pending Org row (list endpoint) and
// fresh at approval time (`preflight_skill_promotion`). The old client-side
// `count + 1 > budget` GUESS was wrong: it always claimed the newly promoted skill
// "won't reach the container", but the interactive Apply actually REJECTS the whole
// push, and the unattended sync keeps the first 25 by name — so the dropped skill
// may be an EXISTING shared one, not the promoted one. We render the server's facts.
//
// The projection shape: { to_scope, promoted_slug, projected_count, budget,
//   at_budget, over_budget, strict_would_fail, dropped_slugs[], promoted_dropped }.
//
// Levels (the approval is ALWAYS allowed — the reviewer decides informed):
//   "over" — after approval the pushable count EXCEEDS the budget: the next
//            interactive Apply FAILS (nothing pushed) and the unattended sync drops
//            the named ordered tail (red).
//   "near" — after approval the count EQUALS the budget: the last free slot is
//            taken (amber) — warned early so the NEXT approval isn't a surprise.
//   null   — room to spare, or no projection (a Role/User target never pushes).
export function formatPushProjection(projection) {
	if (!projection) return null;
	const budget = Number(projection.budget) || 0;
	if (budget <= 0) return null;

	if (projection.over_budget) {
		const dropped = Array.isArray(projection.dropped_slugs) ? projection.dropped_slugs : [];
		const droppedList = dropped.join(", ");
		let tail = "";
		if (projection.promoted_dropped) {
			tail =
				`On the next unattended sync the skill you're approving (${projection.promoted_slug}) ` +
				`is the one dropped, skills sort by name and it falls past the ${budget}-skill cut.`;
		} else if (dropped.length) {
			tail =
				`On the next unattended sync an EXISTING shared skill is dropped instead, ` +
				`${droppedList} (skills sort by name), NOT the one you're approving.`;
		}
		return {
			level: "over",
			projection,
			message:
				`Approving takes the shared catalog to ${projection.projected_count} pushable Org ` +
				`skills, over the ${budget}-skill push budget. The next interactive Apply will FAIL ` +
				`(nothing is pushed) until an Org skill is disabled or removed. ${tail}`.trim(),
		};
	}

	if (projection.at_budget) {
		return {
			level: "near",
			projection,
			message:
				`Approving fills the shared container to ${projection.projected_count} of ${budget} ` +
				`pushable Org skills, the last free slot in the push budget.`,
		};
	}

	return null;
}

// The decision-relevant fingerprint of a projection (mirrors the server's
// `_projection_signature`, R2-SP-5). Two projections with the same signature warn
// the reviewer identically; a changed signature means the shared catalog moved
// under them. Only the fields that drive the warning are compared, so incidental
// fields never register as a change.
export function projectionSignature(projection) {
	if (!projection) return null;
	return JSON.stringify([
		!!projection.over_budget,
		!!projection.at_budget,
		Number(projection.projected_count) || 0,
		Array.isArray(projection.dropped_slugs) ? projection.dropped_slugs : [],
		!!projection.promoted_dropped,
	]);
}

// True when `fresh` warns the reviewer differently from `ack` — used to note "the
// impact changed since the list loaded" before the reviewer confirms an approval
// (the authoritative reconfirm is enforced server-side; this is the client cue).
export function projectionChanged(ack, fresh) {
	return projectionSignature(ack) !== projectionSignature(fresh);
}
