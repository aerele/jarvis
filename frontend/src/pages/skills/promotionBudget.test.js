// Real executable test for the Org-promotion push-budget warning (Skills-area
// promotion surfacing, ruling 2). Plain node built-ins (node:test + node:assert)
// — no external framework, the eventFence.test.js precedent. Run directly
// (`node --test promotionBudget.test.js`) or via the python suite:
// jarvis/tests/test_promotion_budget_client.py subprocess-runs this so the
// budget boundary contract lives in the suite forever. `node --test` exits
// non-zero on any failed assertion, which the python runner asserts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { orgPushBudgetWarning } from "./promotionBudget.js";

const BUDGET = 25;

test("non-Org targets never warn (Role/User never reach the container push)", () => {
	assert.equal(
		orgPushBudgetWarning({ toScope: "Role", pushCount: 100, pushBudget: BUDGET }),
		null
	);
	assert.equal(
		orgPushBudgetWarning({ toScope: "User", pushCount: 100, pushBudget: BUDGET }),
		null
	);
	assert.equal(orgPushBudgetWarning({ toScope: "", pushCount: 100, pushBudget: BUDGET }), null);
});

test("well under budget → null", () => {
	assert.equal(orgPushBudgetWarning({ toScope: "Org", pushCount: 0, pushBudget: BUDGET }), null);
	assert.equal(
		orgPushBudgetWarning({ toScope: "Org", pushCount: 23, pushBudget: BUDGET }),
		null
	);
});

test("filling the last slot (afterApprove === budget) → near", () => {
	const w = orgPushBudgetWarning({ toScope: "Org", pushCount: 24, pushBudget: BUDGET });
	assert.ok(w, "a warning is returned");
	assert.equal(w.level, "near");
	assert.equal(w.afterApprove, 25);
	assert.equal(w.count, 24);
	assert.equal(w.budget, 25);
	assert.match(w.message, /25 of 25/);
});

test("exactly at the cap (afterApprove > budget) → over", () => {
	const w = orgPushBudgetWarning({ toScope: "Org", pushCount: 25, pushBudget: BUDGET });
	assert.ok(w);
	assert.equal(w.level, "over");
	assert.equal(w.afterApprove, 26);
	assert.match(w.message, /over the 25-skill push budget/);
	assert.match(w.message, /will NOT reach the container/);
});

test("well over the cap → over, with honest current count", () => {
	const w = orgPushBudgetWarning({ toScope: "Org", pushCount: 30, pushBudget: BUDGET });
	assert.equal(w.level, "over");
	assert.equal(w.count, 30);
	assert.equal(w.afterApprove, 31);
});

test("a missing/zero budget disables the warning (fail-open, never blocks)", () => {
	assert.equal(orgPushBudgetWarning({ toScope: "Org", pushCount: 100, pushBudget: 0 }), null);
	assert.equal(orgPushBudgetWarning({ toScope: "Org", pushCount: 100 }), null);
	assert.equal(orgPushBudgetWarning(), null);
});

test("non-numeric / negative inputs are coerced defensively", () => {
	// string budget/count (as they might arrive over JSON) still compute
	const w = orgPushBudgetWarning({ toScope: "Org", pushCount: "24", pushBudget: "25" });
	assert.equal(w.level, "near");
	// negative count floors at 0 → afterApprove 1 → null under a real budget
	assert.equal(
		orgPushBudgetWarning({ toScope: "Org", pushCount: -5, pushBudget: BUDGET }),
		null
	);
});
