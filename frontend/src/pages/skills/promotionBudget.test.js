// Real executable test for the Org-promotion push-budget warning FORMATTER
// (Skills-area promotion surfacing, ruling 2 + Codex CDX-SP-2). Plain node
// built-ins (node:test + node:assert) — no external framework, the
// eventFence.test.js precedent. Run directly
// (`node --test promotionBudget.test.js`) or via the python suite:
// jarvis/tests/test_promotion_budget_client.py subprocess-runs this so the
// budget-warning contract lives in the suite forever. `node --test` exits
// non-zero on any failed assertion, which the python runner asserts.
//
// The DISPLACEMENT truth (which skill is dropped, given `skill_name asc` order) is
// computed SERVER-SIDE in custom_skills.project_org_promotion_push and proven in
// jarvis/tests/test_part2_security.py. Here we prove the client RENDERS that
// server projection truthfully — never re-deriving it, never claiming the new
// skill is dropped when the server says an existing one is.
import { test } from "node:test";
import assert from "node:assert/strict";
import { formatPushProjection } from "./promotionBudget.js";

const BUDGET = 25;

test("no projection / non-Org target (null projection) → null", () => {
	assert.equal(formatPushProjection(null), null);
	assert.equal(formatPushProjection(undefined), null);
});

test("a zero/missing budget disables the warning (fail-open, never blocks)", () => {
	assert.equal(
		formatPushProjection({ over_budget: true, budget: 0, dropped_slugs: ["custom-x"] }),
		null
	);
});

test("room to spare (neither over nor at budget) → null", () => {
	assert.equal(
		formatPushProjection({
			budget: BUDGET,
			projected_count: 10,
			at_budget: false,
			over_budget: false,
		}),
		null
	);
});

test("filling the last slot (at_budget) → near", () => {
	const w = formatPushProjection({
		budget: BUDGET,
		projected_count: BUDGET,
		at_budget: true,
		over_budget: false,
	});
	assert.ok(w, "a warning is returned");
	assert.equal(w.level, "near");
	assert.match(w.message, /25 of 25/);
	assert.match(w.message, /last free slot/);
});

test("over budget · strict truth: the interactive Apply FAILS (nothing pushed)", () => {
	const w = formatPushProjection({
		budget: BUDGET,
		projected_count: 26,
		promoted_slug: "custom-zulu",
		over_budget: true,
		strict_would_fail: true,
		dropped_slugs: ["custom-zulu"],
		promoted_dropped: true,
	});
	assert.equal(w.level, "over");
	assert.match(w.message, /interactive Apply will FAIL/);
	assert.match(w.message, /nothing is pushed/);
	assert.match(w.message, /over the 25-skill push budget/);
});

test("displacement: promoted slug sorts AFTER → the PROMOTED skill is the one dropped", () => {
	// Server sorted [custom-aaa, custom-bbb, custom-zulu], cap 2 → drops custom-zulu.
	const w = formatPushProjection({
		budget: 2,
		projected_count: 3,
		promoted_slug: "custom-zulu",
		over_budget: true,
		strict_would_fail: true,
		dropped_slugs: ["custom-zulu"],
		promoted_dropped: true,
	});
	assert.equal(w.level, "over");
	// names the promoted skill as the dropped one — the honest "won't reach" case
	assert.match(w.message, /custom-zulu/);
	assert.match(w.message, /the skill you're approving/);
});

test("omission: promoted slug sorts BEFORE → an EXISTING shared skill is dropped, not the new one", () => {
	// Server sorted [custom-aaa(promoted), custom-bbb, custom-ccc], cap 2 → drops custom-ccc.
	const w = formatPushProjection({
		budget: 2,
		projected_count: 3,
		promoted_slug: "custom-aaa",
		over_budget: true,
		strict_would_fail: true,
		dropped_slugs: ["custom-ccc"],
		promoted_dropped: false,
	});
	assert.equal(w.level, "over");
	// The truthful contract: it must NOT claim the newly promoted skill is dropped.
	assert.match(w.message, /EXISTING shared skill/);
	assert.match(w.message, /custom-ccc/);
	assert.match(w.message, /NOT the one you're approving/);
	assert.doesNotMatch(w.message, /the skill you're approving \(custom-aaa\) is the one dropped/);
});

test("over budget with multiple existing skills dropped → all are named", () => {
	const w = formatPushProjection({
		budget: 2,
		projected_count: 4,
		promoted_slug: "custom-aaa",
		over_budget: true,
		strict_would_fail: true,
		dropped_slugs: ["custom-ccc", "custom-ddd"],
		promoted_dropped: false,
	});
	assert.equal(w.level, "over");
	assert.match(w.message, /custom-ccc, custom-ddd/);
});
