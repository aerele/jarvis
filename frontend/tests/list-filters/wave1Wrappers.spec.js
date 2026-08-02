// P0-01 wrapper-wire contract for the three WAVE-1 feature wrappers. This is the
// test that catches the round-2 defect head-on: api/dashboards.js and
// api/triggers.js rebuilt the request WITHOUT filters_v2, so a Saved Dashboards
// or Triggers clause was visible, entered the URL, bumped the badge — and never
// reached the endpoint. Mocking frappe-ui's `call` and asserting the exact wire
// args (empty + nonempty) is the only place that sees it. (Skills/Macros are in
// wrapperWire.spec.js.)
import { describe, it, expect, vi, beforeEach } from "vitest";

const callDouble = vi.hoisted(() => vi.fn(async () => ({ data: { rows: [], total: 0 } })));
vi.mock("frappe-ui", () => ({ call: callDouble }));

import { listDashboardsPage } from "@/api/dashboards";
import { listTriggersPage } from "@/api/triggers";
import { listWikiPagesPage } from "@/api/wiki";

const DASH = [
	{ doctype: "Jarvis Dashboard", fieldname: "description", operator: "like", value: "%q%" },
];
const TRIG = [
	{ doctype: "Jarvis Trigger", fieldname: "description", operator: "like", value: "%q%" },
];
const WIKI = [
	{ doctype: "Jarvis Wiki Page", fieldname: "summary", operator: "like", value: "%q%" },
];

beforeEach(() => callDouble.mockClear());

describe("wave-1 feature wrappers send filters_v2 (P0-01)", () => {
	it("dashboards: omits when empty, JSON-serializes when present", async () => {
		await listDashboardsPage({ search: "s", filters_v2: [] });
		let [method, args] = callDouble.mock.calls.at(-1);
		expect(method).toBe("jarvis.chat.dashboards_api.list_dashboards_page");
		expect(args).not.toHaveProperty("filters_v2");
		expect(args.search).toBe("s");

		await listDashboardsPage({ filters_v2: DASH });
		[, args] = callDouble.mock.calls.at(-1);
		expect(args.filters_v2).toBe(JSON.stringify(DASH));
	});

	it("triggers: omits when empty, JSON-serializes when present", async () => {
		await listTriggersPage({ filters_v2: [] });
		let [method, args] = callDouble.mock.calls.at(-1);
		expect(method).toBe("jarvis.chat.triggers_api.list_triggers_page");
		expect(args).not.toHaveProperty("filters_v2");

		await listTriggersPage({ filters_v2: TRIG });
		[, args] = callDouble.mock.calls.at(-1);
		expect(args.filters_v2).toBe(JSON.stringify(TRIG));
	});

	it("wiki: keeps its bespoke shape and adds filters_v2 only when present", async () => {
		await listWikiPagesPage({ page: 2, filters_v2: [] });
		let [method, args] = callDouble.mock.calls.at(-1);
		expect(method).toBe("jarvis.chat.wiki.list_wiki_pages_page");
		expect(args.page).toBe(2); // bespoke shape preserved
		expect(args.scope_filter).toBe("all");
		expect(args).not.toHaveProperty("filters_v2");

		await listWikiPagesPage({ filters_v2: WIKI });
		[, args] = callDouble.mock.calls.at(-1);
		expect(args.filters_v2).toBe(JSON.stringify(WIKI));
	});
});
