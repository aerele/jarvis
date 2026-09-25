import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * An approved Org promotion (or an insight applied to an Org skill) must reach
 * the assistant right away.
 *
 * Neither server endpoint pushes: the push restarts the shared container and the
 * approval runs under the catalog lock. Both return `needs_apply` instead, and
 * the Review page runs the Apply. Before this, both sides said the change would
 * ride "the next skills push" and nothing ever sent one, so an approved skill sat
 * unpushed until an unrelated restart. These tests pin both halves of that
 * contract, since dropping either one silently brings the gap back.
 */

const APP = path.resolve(__dirname, "../../../..");
const read = (p) => fs.readFileSync(path.join(APP, p), "utf8");

const reviewTab = read("frontend/src/pages/skills/ReviewTab.vue");
const insightDialog = read("frontend/src/components/learning/InsightApplyDialog.vue");
const skillsPy = read("jarvis/chat/custom_skills_api.py");
const learnedPy = read("jarvis/chat/learned_api.py");

const fnBody = (src, sig) => {
	const fn = src.slice(src.indexOf(sig));
	return fn.slice(0, fn.indexOf("\n}"));
};

describe("apply after approve: servers flag it, the client runs it", () => {
	it("both server endpoints report needs_apply from the push-set rule", () => {
		expect(skillsPy).toContain('out["needs_apply"] = is_pushable_skill(out["materialized"])');
		expect(learnedPy).toContain('"needs_apply": is_pushable_skill(row_name)');
	});

	it("an approval with needs_apply starts the push on the queue's pill", () => {
		const body = fnBody(reviewTab, "async function decideSkillPromo(");
		expect(body).toMatch(/if \(approve && r && r\.needs_apply\) promoSyncPill\.value/);
		expect(body).toContain("promoSyncPill.value.apply()");
		expect(reviewTab).toContain('<SyncPill ref="promoSyncPill" />');
	});

	it("an applied insight with needs_apply runs the push", () => {
		expect(reviewTab).toContain('@applied="onInsightApplied"');
		const body = fnBody(reviewTab, "async function onInsightApplied(");
		expect(body).toContain("e.needs_apply");
		expect(body).toContain("await applyCustomSkills()");
		expect(insightDialog).toContain(
			'emit("applied", { skill_name: skill, needs_apply: needsApply })'
		);
	});

	it("no copy still promises a push that never comes", () => {
		expect(insightDialog).not.toContain("next skills push");
	});
});
