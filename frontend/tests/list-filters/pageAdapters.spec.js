// S6: the PAGE adapters, not just the wrappers. wave1Wrappers/wrapperWire prove
// the feature wrappers serialize filters_v2; they do NOT prove the page's own
// `fetchFn` adapter forwards it INTO the wrapper. WikiTab maps parameters
// EXPLICITLY (`filters_v2: p.filters_v2`) — delete that line and every wrapper
// test still passes while the panel silently stops filtering. The four other
// migrated pages forward it through a `{ ...p }` spread. This spec pins both: the
// extracted adapter units, and the FULL chain (real adapter → real wrapper →
// frappe-ui `call`), so "proves the chain" is actually true.
import { describe, it, expect, vi, beforeEach } from "vitest";

const callDouble = vi.hoisted(() => vi.fn(async () => ({ data: { rows: [], total: 0 } })));
vi.mock("frappe-ui", () => ({ call: callDouble }));

import { buildWikiListArgs, searchSplitArgs } from "@/pages/list/listAdapters";
import { listWikiPagesPage } from "@/api/wiki";
import { listCustomSkillsPage } from "@/api";

const CLAUSES = [
	{ doctype: "Jarvis Wiki Page", fieldname: "summary", operator: "like", value: "%q%" },
];

beforeEach(() => callDouble.mockClear());

describe("page adapters forward filters_v2 (S6)", () => {
	// ── the extracted units ──────────────────────────────────────────────────
	it("buildWikiListArgs carries filters_v2 through the EXPLICIT mapping", () => {
		// This is the assertion that turns red the moment `filters_v2: p.filters_v2`
		// is removed from the wiki adapter.
		const args = buildWikiListArgs({
			filters_v2: CLAUSES,
			filters: {},
			start: 0,
			page_length: 20,
		});
		expect(args.filters_v2).toBe(CLAUSES);
		// and it still builds its bespoke page-number shape
		expect(args.page).toBe(1);
	});

	it("searchSplitArgs carries filters_v2 through the SPREAD (spread-safety)", () => {
		const args = searchSplitArgs({
			filters_v2: CLAUSES,
			filters: { search: "q", scope: "mine" },
			start: 20,
		});
		expect(args.filters_v2).toBe(CLAUSES);
		// search is lifted out of the whitelisted filters object…
		expect(args.search).toBe("q");
		// …and the rest of the filters survive, minus search
		expect(args.filters).toEqual({ scope: "mine" });
	});

	it("searchSplitArgs is unharmed when there are no clauses", () => {
		const args = searchSplitArgs({ filters: {}, filters_v2: [] });
		expect(args.filters_v2).toEqual([]);
	});

	// ── the FULL chain: adapter → real wrapper → `call` ──────────────────────
	it("wiki: the explicit adapter's filters_v2 reaches the endpoint", async () => {
		await listWikiPagesPage(buildWikiListArgs({ filters_v2: CLAUSES, filters: {} }));
		const [, args] = callDouble.mock.calls.at(-1);
		expect(args.filters_v2).toBe(JSON.stringify(CLAUSES));
	});

	it("skills: the spread adapter's filters_v2 reaches the endpoint", async () => {
		const skillClauses = [
			{
				doctype: "Jarvis Custom Skill",
				fieldname: "description",
				operator: "like",
				value: "%q%",
			},
		];
		await listCustomSkillsPage(
			searchSplitArgs({ filters_v2: skillClauses, filters: { search: "q" } })
		);
		const [, args] = callDouble.mock.calls.at(-1);
		expect(args.filters_v2).toBe(JSON.stringify(skillClauses));
		expect(args.search).toBe("q");
	});

	it("wiki: no filters_v2 key at all when the clause set is empty", async () => {
		await listWikiPagesPage(buildWikiListArgs({ filters_v2: [], filters: {} }));
		const [, args] = callDouble.mock.calls.at(-1);
		expect(args).not.toHaveProperty("filters_v2");
	});
});
