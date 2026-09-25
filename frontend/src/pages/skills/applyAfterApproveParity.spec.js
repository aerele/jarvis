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
	it("both server endpoints report needs_apply", () => {
		expect(skillsPy).toContain(
			'out["needs_apply"] = _approval_needs_apply(req.to_scope, out.get("push_projection"))'
		);
		expect(learnedPy).toContain('"needs_apply": apply_would_push(row_name)');
	});

	it("an approval or applied insight with needs_apply runs the push", () => {
		const decide = fnBody(reviewTab, "async function decideSkillPromo(");
		expect(decide).toContain("if (approve && r && r.needs_apply) pushSharedSkills();");
		expect(reviewTab).toContain('@applied="onInsightApplied"');
		const insight = fnBody(reviewTab, "function onInsightApplied(");
		expect(insight).toContain("if (e && e.needs_apply) pushSharedSkills();");
		expect(insightDialog).toContain(
			'emit("applied", { skill_name: skill, needs_apply: needsApply })'
		);
	});

	it("the push never depends on a mounted pill", () => {
		// A queue switch mid-request used to unmount the pill and drop the push.
		const push = fnBody(reviewTab, "async function pushSharedSkills(");
		expect(push.indexOf("await applyCustomSkills()")).toBeGreaterThan(-1);
		expect(push.indexOf("await applyCustomSkills()")).toBeLessThan(
			push.indexOf("syncPill.value")
		);
		// one always-mounted pill, outside the per-queue templates
		expect((reviewTab.match(/<SyncPill /g) || []).length).toBe(1);
		const pillAt = reviewTab.indexOf('<SyncPill ref="syncPill"');
		expect(pillAt).toBeLessThan(
			reviewTab.indexOf("<template v-if=\"queueType === 'candidates'\">")
		);
	});

	it("no copy still promises a push that never comes", () => {
		const flat = (t) => t.replace(/\s+/g, " ");
		expect(flat(insightDialog)).not.toContain("next skills push");
		expect(flat(insightDialog)).toContain("updates your assistant");
	});
});
