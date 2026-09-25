// Plain node built-ins (node:test + node:assert), the promotionBudget.test.js
// precedent - no external framework, no DOM. Proves the ONE scope-ladder rank
// both promotion UI surfaces (SkillDetail.vue, PromotionRequestDialog.vue) now
// import, instead of each keeping (and silently drifting from) its own copy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { SCOPE_RANK, isWiderScope } from "./promotionScope.js";

test("ranks the ladder User < Role < Org", () => {
	assert.ok(SCOPE_RANK.User < SCOPE_RANK.Role);
	assert.ok(SCOPE_RANK.Role < SCOPE_RANK.Org);
});

test("Org is wider than every real scope, and wider than nothing beyond itself", () => {
	assert.equal(isWiderScope("Org", "User"), true);
	assert.equal(isWiderScope("Org", "Role"), true);
	assert.equal(isWiderScope("Org", "Org"), false);
});

test("Role is wider than User only", () => {
	assert.equal(isWiderScope("Role", "User"), true);
	assert.equal(isWiderScope("Role", "Role"), false);
	assert.equal(isWiderScope("Role", "Org"), false);
});

test("an unset/unrecognised baseline (the wiki dialog's default) never hides a real target", () => {
	assert.equal(isWiderScope("Role", ""), true);
	assert.equal(isWiderScope("Org", ""), true);
	assert.equal(isWiderScope("Role", undefined), true);
});
