import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * The wire format of the Skills Admin calls: the endpoint's names, the filters as
 * JSON, the tab's `audience` sent as the server's `scope`, and `restore` as 0 or 1.
 */

const call = vi.hoisted(() => vi.fn(async () => ({})));
vi.mock("frappe-ui", () => ({ call }));

import * as api from "./skillsAdmin";
import { dismissSkillNotices } from "./skills";

const SA = "jarvis.chat.skills_admin_api.";
const sent = () => call.mock.calls.at(-1);

beforeEach(() => call.mockClear());

describe("skills admin api", () => {
	it("lists a page, with the filters as JSON and the page size it wants", async () => {
		await api.adminListSkills({
			search: "bills",
			filters: { owner: "a@x.test" },
			page: 3,
			pageSize: 20,
		});
		expect(sent()).toEqual([
			SA + "admin_list_skills",
			{ filters: '{"owner":"a@x.test"}', search: "bills", page: 3, page_size: 20 },
		]);
	});

	it("asks for the first twenty with nothing given", async () => {
		await api.adminListSkills();
		expect(sent()).toEqual([
			SA + "admin_list_skills",
			{ filters: "{}", search: "", page: 1, page_size: 20 },
		]);
	});

	it("sends the audience filter under the server's name for it", async () => {
		await api.adminListSkills({ filters: { audience: "Org", armed: 1, enabled: 0 } });
		expect(JSON.parse(sent()[1].filters)).toEqual({ scope: "Org", armed: 1, enabled: 0 });
	});

	it("asks for the owners, one skill, and the three acts by the names the server has", async () => {
		await api.adminSkillOwners("as");
		expect(sent()).toEqual([SA + "admin_skill_owners", { search: "as" }]);
		await api.adminGetSkill("s1");
		expect(sent()).toEqual([SA + "admin_get_skill", { name: "s1" }]);
		await api.adminHold("s1", "Why");
		expect(sent()).toEqual([SA + "admin_hold", { skill: "s1", reason: "Why" }]);
		await api.adminDelete("s1");
		expect(sent()).toEqual([SA + "admin_delete", { skill: "s1" }]);
	});

	it("sends restore as 0 or 1", async () => {
		await api.adminRelease("s1");
		expect(sent()).toEqual([SA + "admin_release", { skill: "s1", restore: 0 }]);
		await api.adminRelease("s1", true);
		expect(sent()).toEqual([SA + "admin_release", { skill: "s1", restore: 1 }]);
	});

	it("asks for hand over targets with the skill, and hands over with the owner shown", async () => {
		await api.adminHandoverTargets("be", "s1");
		expect(sent()).toEqual([SA + "admin_handover_targets", { search: "be", skill: "s1" }]);
		await api.adminHandoverTargets();
		expect(sent()).toEqual([SA + "admin_handover_targets", { search: "" }]);
		await api.adminHandover("s1", "ben@x.test", "asha@x.test");
		expect(sent()).toEqual([
			SA + "admin_handover",
			{ skill: "s1", new_owner: "ben@x.test", expected_owner: "asha@x.test" },
		]);
	});

	it("dismisses owner notices by name, as a JSON list", async () => {
		await dismissSkillNotices(["N1", "N2"]);
		expect(sent()).toEqual([
			"jarvis.chat.custom_skills_api.dismiss_skill_notices",
			{ names: '["N1","N2"]' },
		]);
	});
});
