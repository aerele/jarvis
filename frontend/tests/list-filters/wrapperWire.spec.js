// P0-01 wrapper-wire contract: the exact args each MIGRATED list wrapper hands to
// frappe-ui's `call`. This is the gap the round-2 review found — composable and
// server tests both passed while api/dashboards.js and api/triggers.js rebuilt
// the request WITHOUT filters_v2, so a canonical clause silently never reached
// the endpoint. Mocking `call` at this boundary is the ONE test that catches it.
//
// This file covers the two api.js wrappers (Skills, Macros); the three feature
// wrappers (Dashboards, Triggers, Wiki) are covered by wave1Wrappers.spec.js on
// the branch that owns those files.
import { describe, it, expect, vi, beforeEach } from "vitest";

const callDouble = vi.hoisted(() => vi.fn(async () => ({ rows: [], total: 0 })));
vi.mock("frappe-ui", () => ({ call: callDouble }));

import { listCustomSkillsPage, listMacrosPage } from "@/api";

const CLAUSES = [
	{ doctype: "Jarvis Custom Skill", fieldname: "description", operator: "like", value: "%x%" },
];

beforeEach(() => callDouble.mockClear());

describe("api.js list wrappers send filters_v2 (P0-01)", () => {
	const cases = [
		["skills", listCustomSkillsPage, "jarvis.chat.custom_skills_api.list_custom_skills_page"],
		["macros", listMacrosPage, "jarvis.chat.macros_api.list_macros_page"],
	];

	for (const [name, wrapper, method] of cases) {
		it(`${name}: omits filters_v2 when there are no clauses`, async () => {
			await wrapper({ search: "hi", filters_v2: [] });
			const [calledMethod, args] = callDouble.mock.calls.at(-1);
			expect(calledMethod).toBe(method);
			expect(args).not.toHaveProperty("filters_v2");
			expect(args.search).toBe("hi");
		});

		it(`${name}: JSON-serializes filters_v2 when clauses are present`, async () => {
			await wrapper({ filters_v2: CLAUSES });
			const [, args] = callDouble.mock.calls.at(-1);
			expect(args.filters_v2).toBe(JSON.stringify(CLAUSES));
		});
	}
});
