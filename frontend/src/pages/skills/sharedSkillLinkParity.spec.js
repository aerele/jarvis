import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * 5c: the Review tab's way in to a shared skill's form. An approved promotion row
 * carries `shared_skill` (the live Role or company copy) from the server, and the
 * card opens that row, not the requester's private source, on SkillDetail. Read from
 * source, as the other Review tab parity specs are: mounting the tab is heavy.
 */

const APP = path.resolve(__dirname, "../../../..");
const read = (p) => fs.readFileSync(path.join(APP, p), "utf8");

const reviewTab = read("frontend/src/pages/skills/ReviewTab.vue");
const skillsPy = read("jarvis/chat/custom_skills_api.py");

describe("the Review tab links an approved promotion to its shared copy", () => {
	it("the server sends the shared copy on an approved row only", () => {
		expect(skillsPy).toContain('"shared_skill": shared_copies.get(');
		expect(skillsPy).toMatch(/if r\.get\("status"\) == "Approved"/);
	});

	it("the card shows the link only when there is a copy, and opens it", () => {
		expect(reviewTab).toMatch(
			/v-if="p\.shared_skill"[\s\S]{0,200}@click="openSharedSkill\(p\)"/
		);
		const fn = reviewTab.slice(reviewTab.indexOf("function openSharedSkill(p) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain(
			'router.push({ name: "SkillDetail", params: { id: p.shared_skill } })'
		);
	});
});
