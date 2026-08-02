import { describe, it, expect, vi, beforeEach } from "vitest";
import { defineComponent, reactive, nextTick } from "vue";
import { mount, flushPromises } from "@vue/test-utils";
import { SKILLS_SCHEMA, DESCRIPTION, ENABLED, CREATION } from "./fixtures.js";

const toastDouble = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), create: vi.fn() }));
vi.mock("frappe-ui", () => ({ toast: toastDouble }));
vi.mock("@/api", () => ({ getListFilterSchema: vi.fn(), searchLink: vi.fn() }));
// useStorage would otherwise persist page length across specs in one jsdom.
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (key, value) => ref(value) };
});

import { useListPage } from "@/composables/useListPage";
import { clauseForEntry, URL_PARAM } from "@/components/list/filterModel";

/**
 * A router double that behaves like the real one where it matters: `replace`
 * mutates the SAME reactive query object the composable reads, so the watcher
 * sees our own write and must recognise it; and `back()` restores a previous
 * query, which is the browser-history path.
 */
function routerDouble(initialQuery = {}) {
	const route = reactive({ query: { ...initialQuery } });
	const history = [{ ...initialQuery }];
	const router = {
		replace: vi.fn(({ query }) => {
			history.push({ ...query });
			Object.keys(route.query).forEach((k) => delete route.query[k]);
			Object.assign(route.query, query);
			return Promise.resolve();
		}),
	};
	function navigate(query) {
		Object.keys(route.query).forEach((k) => delete route.query[k]);
		Object.assign(route.query, query);
	}
	return { route, router, history, navigate };
}

/** Mount the composable inside a real component so onMounted/watch actually run. */
function host(options) {
	let api = null;
	const Host = defineComponent({
		setup() {
			api = useListPage(options);
			return () => null;
		},
	});
	const wrapper = mount(Host);
	return { api, wrapper };
}

const clause = (entry, value, extra = {}) => ({ ...clauseForEntry(entry), value, ...extra });

function envelopeError(code, message) {
	const e = new Error("rejected");
	e.status = code === "list_filter_schema_unavailable" ? 503 : 417;
	e.messages = [message, { ok: false, error: { code, message } }];
	return e;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("request generation", () => {
	it("sends no filters_v2 at all until a clause is complete", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const { api } = host({ fetchFn, storageKey: "t1", viewKey: "skills" });
		await flushPromises();
		expect(fetchFn.mock.calls[0][0].filters_v2).toEqual([]);

		// a pending row (Between with one bound) is NOT a filter
		await api.setClauses([clause(CREATION, ["2026-01-01", ""])]);
		expect(fetchFn.mock.calls[1][0].filters_v2).toEqual([]);

		await api.setClauses([clause(CREATION, ["2026-01-01", "2026-02-01"])]);
		expect(fetchFn.mock.calls[2][0].filters_v2).toEqual([
			{
				doctype: "Jarvis Custom Skill",
				fieldname: "creation",
				operator: "Between",
				value: ["2026-01-01", "2026-02-01"],
			},
		]);
	});

	it("keeps the legacy filters object alongside, for the compatibility window", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const { api } = host({
			fetchFn,
			storageKey: "t2",
			viewKey: "skills",
			initialFilters: { scope: "mine" },
		});
		await flushPromises();
		await api.setClauses([clause(DESCRIPTION, "month")]);
		const sent = fetchFn.mock.calls.at(-1)[0];
		expect(sent.filters).toEqual({ scope: "mine" });
		expect(sent.filters_v2).toHaveLength(1);
	});

	it("falls back to legacy transport when the view is rolled back (P1-05)", async () => {
		// The server flag turned filters_v2 off for this view: its schema endpoint
		// declines with view_not_filterable. The list must still load, sending NO
		// filters_v2 (legacy transport) — never a coded rejection read as empty.
		const fetchFn = vi.fn(async () => ({ rows: [{ name: "a" }], total: 1 }));
		const fetchSchema = vi.fn(async () => ({
			ok: false,
			error: { code: "list_filter_view_not_filterable", message: "off" },
		}));
		const { api } = host({ fetchFn, storageKey: "t3b", viewKey: "macros", fetchSchema });
		await flushPromises();
		// A clause is set, but the rollback means it never reaches the wire.
		await api.requestSchema();
		await api.setClauses([clause(DESCRIPTION, "x")]);
		await flushPromises();
		expect(fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([]);
	});

	it("resets to page one on any clause change", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [{ name: "a" }], total: 9, has_more: true }));
		const { api } = host({ fetchFn, storageKey: "t3", viewKey: "skills" });
		await flushPromises();
		await api.loadMore();
		expect(fetchFn.mock.calls.at(-1)[0].start).toBe(1);
		await api.setClauses([clause(DESCRIPTION, "x")]);
		expect(fetchFn.mock.calls.at(-1)[0].start).toBe(0);
	});
});

describe("stale-response fencing", () => {
	// The defect: a slow first request resolving AFTER a newer one and repainting
	// the list with rows for a filter the user has already replaced.
	it("drops a response superseded by a newer request", async () => {
		const settle = [];
		const fetchFn = vi.fn(() => new Promise((resolve) => settle.push(resolve)));
		const { api } = host({ fetchFn, storageKey: "t4", viewKey: "skills" });
		await nextTick();
		api.setClauses([clause(DESCRIPTION, "second")]);
		await nextTick();
		expect(settle).toHaveLength(2);

		settle[1]({ rows: [{ name: "fresh" }], total: 1 });
		await flushPromises();
		settle[0]({ rows: [{ name: "stale" }], total: 99 });
		await flushPromises();

		expect(api.rows.value.map((r) => r.name)).toEqual(["fresh"]);
		expect(api.total.value).toBe(1);
	});

	it("drops a stale FAILURE too, so an old error cannot overwrite a good list", async () => {
		const settle = [];
		const fetchFn = vi.fn(
			() => new Promise((resolve, reject) => settle.push({ resolve, reject }))
		);
		const { api } = host({ fetchFn, storageKey: "t5", viewKey: "skills" });
		await nextTick();
		api.setClauses([clause(DESCRIPTION, "second")]);
		await nextTick();

		settle[1].resolve({ rows: [{ name: "fresh" }], total: 1 });
		await flushPromises();
		settle[0].reject(envelopeError("list_filter_invalid_value", "Description needs a value."));
		await flushPromises();

		expect(api.rows.value.map((r) => r.name)).toEqual(["fresh"]);
		expect(api.filterError.value).toBeNull();
	});
});

describe("URL state", () => {
	it("writes the versioned param on change and clears it when the last clause goes", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const { route, router } = routerDouble();
		const { api } = host({ fetchFn, storageKey: "u1", viewKey: "skills", route, router });
		await flushPromises();

		await api.setClauses([clause(DESCRIPTION, "month end")]);
		expect(JSON.parse(route.query[URL_PARAM])).toEqual({
			v: 1,
			k: "skills",
			c: [["Jarvis Custom Skill", "description", "like", "month end"]],
		});
		// replace, not push: a filter edit is a refinement, not a history step
		expect(router.replace).toHaveBeenCalled();

		await api.setClauses([]);
		expect(route.query[URL_PARAM]).toBeUndefined();
	});

	it("applies a shared link's clauses to the FIRST request, after checking the catalog", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const param = JSON.stringify({
			v: 1,
			k: "skills",
			c: [["Jarvis Custom Skill", "description", "like", "month end"]],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		host({ fetchFn, storageKey: "u2", viewKey: "skills", route, router, fetchSchema });
		await flushPromises();

		expect(fetchSchema).toHaveBeenCalledWith("skills");
		expect(fetchFn).toHaveBeenCalledTimes(1);
		expect(fetchFn.mock.calls[0][0].filters_v2).toEqual([
			{
				doctype: "Jarvis Custom Skill",
				fieldname: "description",
				operator: "like",
				value: "month end",
			},
		]);
	});

	// plan §8 step 4: dropped clauses are REPORTED, never silently reinterpreted.
	it("drops a clause this caller may no longer use and says so, visibly", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const param = JSON.stringify({
			v: 1,
			k: "skills",
			c: [
				["Jarvis Custom Skill", "description", "like", "keep"],
				// permlevel-1 field: absent from a plain user's catalog entirely
				["Jarvis Custom Skill", "skill_bundle", "like", "secret"],
			],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const { api } = host({
			fetchFn,
			storageKey: "u3",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();

		expect(api.filterNotice.value).toBe(
			"1 filter from this link is no longer available on this list and was removed."
		);
		expect(fetchFn.mock.calls[0][0].filters_v2.map((c) => c.fieldname)).toEqual([
			"description",
		]);
		// the URL is rewritten to what is actually in force
		expect(JSON.parse(route.query[URL_PARAM]).c).toHaveLength(1);

		api.dismissFilterNotice();
		expect(api.filterNotice.value).toBe("");
	});

	it("still sends the link's clauses when the catalog itself is unavailable", async () => {
		// Dropping them here would show an UNfiltered list for a filtered URL; the
		// server re-validates and answers with a code if they are wrong.
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => {
			throw envelopeError("list_filter_schema_unavailable", "Filters are unavailable.");
		});
		const param = JSON.stringify({
			v: 1,
			k: "skills",
			c: [["Jarvis Custom Skill", "description", "like", "month end"]],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const { api } = host({
			fetchFn,
			storageKey: "u4",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();

		expect(api.filterSchemaState.value).toBe("error");
		expect(fetchFn.mock.calls[0][0].filters_v2).toHaveLength(1);
	});

	it("follows browser back/forward without looping on its own writes", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const { route, router, navigate } = routerDouble();
		const { api } = host({
			fetchFn,
			storageKey: "u5",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();

		await api.setClauses([clause(DESCRIPTION, "month end")]);
		await flushPromises();
		const writes = router.replace.mock.calls.length;
		const calls = fetchFn.mock.calls.length;
		await nextTick();
		// our own write must not re-enter the watcher
		expect(router.replace.mock.calls.length).toBe(writes);
		expect(fetchFn.mock.calls.length).toBe(calls);

		// A navigation to a DIFFERENT payload is honoured
		navigate({
			[URL_PARAM]: JSON.stringify({
				v: 1,
				k: "skills",
				c: [["Jarvis Custom Skill", "description", "like", "back again"]],
			}),
		});
		await flushPromises();
		expect(fetchFn.mock.calls.at(-1)[0].filters_v2[0].value).toBe("back again");
	});

	// P1-1: inside one mounted list, a transition to NO param is a navigation
	// that dropped the query (a tab switch resolves `{name}`/`{hash}` without
	// it), never an intentional clear — an intentional clear goes through
	// setClauses. The composable re-asserts what is actually in force.
	it("ignores a transition-to-empty it did not author, and puts the param back", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const { route, router, navigate } = routerDouble();
		const { api } = host({
			fetchFn,
			storageKey: "u5b",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();
		await api.setClauses([clause(DESCRIPTION, "month end")]);
		await flushPromises();
		const fetches = fetchFn.mock.calls.length;

		navigate({}); // the tab switch
		await flushPromises();

		expect(api.filterClauses.value).toHaveLength(1);
		expect(JSON.parse(route.query[URL_PARAM]).c[0][3]).toBe("month end");
		expect(fetchFn.mock.calls.length).toBe(fetches); // and no wasted refetch
	});

	it("still honours an authored clear", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const { route, router } = routerDouble();
		const { api } = host({
			fetchFn,
			storageKey: "u5c",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();
		await api.setClauses([clause(DESCRIPTION, "month end")]);
		await api.setClauses([]);
		await flushPromises();
		expect(route.query[URL_PARAM]).toBeUndefined();
		expect(api.filterClauses.value).toEqual([]);
		expect(fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([]);
	});

	it("ignores a payload a sibling tab on the same route wrote", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const param = JSON.stringify({
			v: 1,
			k: "learning",
			c: [["Jarvis Learned Pattern", "domain", "=", "sales"]],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const { api } = host({ fetchFn, storageKey: "u6", viewKey: "skills", route, router });
		await flushPromises();
		expect(api.filterClauses.value).toEqual([]);
		expect(fetchFn.mock.calls[0][0].filters_v2).toEqual([]);
	});

	it("leaves the URL alone entirely for an unmigrated list", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const { route, router } = routerDouble();
		const { api } = host({ fetchFn, storageKey: "u7", route, router }); // no viewKey
		await flushPromises();
		await api.setFilters({ enabled: "1" });
		expect(router.replace).not.toHaveBeenCalled();
		expect(route.query[URL_PARAM]).toBeUndefined();
	});
});

describe("quick filters ⇄ clauses", () => {
	const options = (extra = {}) => ({
		fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
		storageKey: "q",
		viewKey: "skills",
		quickClauses: { enabled: "enabled" },
		fetchSchema: vi.fn(async () => SKILLS_SCHEMA),
		...extra,
	});

	it("stays purely legacy until the catalog has loaded", async () => {
		const opts = options();
		const { api } = host(opts);
		await flushPromises();
		await api.setFilters({ enabled: "1" });
		expect(api.filterClauses.value).toEqual([]);
		expect(opts.fetchFn.mock.calls.at(-1)[0].filters).toEqual({ enabled: "1" });
	});

	it("adopts what the toolbar already shows when the panel is first opened", async () => {
		const opts = options({ initialFilters: { enabled: "1", scope: "mine" } });
		const { api } = host(opts);
		await flushPromises();
		await api.requestSchema();
		expect(api.filterClauses.value).toHaveLength(1);
		expect(api.filterClauses.value[0]).toMatchObject({
			doctype: "Jarvis Custom Skill",
			fieldname: "enabled",
			operator: "=",
			value: "1",
		});
		// `scope` is an ownership pseudo-filter with no canonical field: untouched
		expect(api.filters.scope).toBe("mine");
	});

	it("materializes a quick change into a canonical clause, replacing that field", async () => {
		const opts = options();
		const { api } = host(opts);
		await flushPromises();
		await api.requestSchema();
		await api.setFilters({ enabled: "1" });
		expect(api.filterClauses.value).toHaveLength(1);
		await api.setFilters({ enabled: "0" });
		expect(api.filterClauses.value).toHaveLength(1);
		expect(api.filterClauses.value[0].value).toBe("0");
		await api.setFilters({});
		expect(api.filterClauses.value).toEqual([]);
	});

	it("reflects a panel edit back into the quick control", async () => {
		const opts = options();
		const { api } = host(opts);
		await flushPromises();
		await api.requestSchema();
		await api.setClauses([clause(ENABLED, "1")]);
		expect(api.filters.enabled).toBe("1");
		await api.setClauses([]);
		expect(api.filters.enabled).toBeUndefined();
	});

	it("blanks the quick control when the panel says something it cannot express", async () => {
		const opts = options();
		const { api } = host(opts);
		await flushPromises();
		await api.requestSchema();
		// two clauses on the one field: no single select value means that
		await api.setClauses([clause(ENABLED, "1"), clause(ENABLED, "0")]);
		expect(api.filters.enabled).toBeUndefined();
		expect(api.filterClauses.value).toHaveLength(2);
	});

	it("does not let one quick control's edit wipe a clause on another field", async () => {
		const opts = options();
		const { api } = host(opts);
		await flushPromises();
		await api.requestSchema();
		await api.setClauses([clause(DESCRIPTION, "month end")]);
		await api.setFilters({ enabled: "1" });
		expect(api.filterClauses.value.map((c) => c.fieldname).sort()).toEqual([
			"description",
			"enabled",
		]);
	});
});

describe("coded rejections (never a dead-end toast)", () => {
	it("surfaces the mapped copy instead of the generic error toast", async () => {
		const fetchFn = vi.fn(async () => {
			throw envelopeError(
				"list_filter_invalid_value",
				"Created On needs a start and an end."
			);
		});
		const { api } = host({ fetchFn, storageKey: "e1", viewKey: "skills" });
		await flushPromises();
		expect(api.filterError.value).toEqual({
			code: "list_filter_invalid_value",
			kind: "row",
			message: "Created On needs a start and an end.",
		});
		expect(api.error.value).toBe("Created On needs a start and an end.");
		expect(toastDouble.error).not.toHaveBeenCalled();
	});

	it("keeps the ordinary toast for an ordinary failure", async () => {
		const e = new Error("boom");
		e.messages = ["Something went wrong."];
		const fetchFn = vi.fn(async () => {
			throw e;
		});
		const { api } = host({ fetchFn, storageKey: "e2", viewKey: "skills" });
		await flushPromises();
		expect(api.filterError.value).toBeNull();
		expect(toastDouble.error).toHaveBeenCalledWith("Something went wrong.");
	});

	it("refetches the catalog and drops what vanished when the field is unknown", async () => {
		let schema = SKILLS_SCHEMA;
		const fetchSchema = vi.fn(async () => schema);
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const { api } = host({ fetchFn, storageKey: "e3", viewKey: "skills", fetchSchema });
		await flushPromises();
		await api.requestSchema();
		await api.setClauses([clause(DESCRIPTION, "x")]);
		expect(api.filterClauses.value).toHaveLength(1);

		// the field is gone from the caller's catalog now
		schema = {
			...SKILLS_SCHEMA,
			fields: SKILLS_SCHEMA.fields.filter((f) => f.fieldname !== "description"),
		};
		fetchFn.mockImplementationOnce(async () => {
			throw envelopeError(
				"list_filter_unknown_field",
				"That field cannot be filtered on this list."
			);
		});
		await api.resetLoad();
		await flushPromises();

		expect(fetchSchema).toHaveBeenCalledTimes(2);
		expect(api.filterClauses.value).toEqual([]);
		expect(api.filterNotice.value).toMatch(/no longer available/);
		// and it RELOADS, so the user is not left staring at a rejection with no
		// way out but Clear all
		expect(api.filterError.value).toBeNull();
		expect(fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([]);
	});

	it("reads a rejection that arrives as a resolved envelope as a rejection", async () => {
		const fetchFn = vi.fn(async () => ({
			ok: false,
			error: {
				code: "list_filter_too_many_clauses",
				message: "At most 20 filters at a time.",
			},
		}));
		const { api } = host({ fetchFn, storageKey: "e4", viewKey: "skills" });
		await flushPromises();
		expect(api.filterError.value.kind).toBe("cap");
		expect(api.rows.value).toEqual([]); // never read as an empty page
	});

	it("clears the rejection once a request succeeds", async () => {
		let fail = true;
		const fetchFn = vi.fn(async () => {
			if (fail)
				throw envelopeError("list_filter_invalid_value", "Description needs a value.");
			return { rows: [{ name: "a" }], total: 1 };
		});
		const { api } = host({ fetchFn, storageKey: "e5", viewKey: "skills" });
		await flushPromises();
		expect(api.filterError.value).not.toBeNull();
		fail = false;
		await api.resetLoad();
		expect(api.filterError.value).toBeNull();
		expect(api.error.value).toBe("");
	});
});

describe("schema fetching", () => {
	it("fetches once and only when asked", async () => {
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const { api } = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "s1",
			viewKey: "skills",
			fetchSchema,
		});
		await flushPromises();
		expect(fetchSchema).not.toHaveBeenCalled();
		await api.requestSchema();
		await api.requestSchema();
		expect(fetchSchema).toHaveBeenCalledTimes(1);
		expect(api.filterSchemaState.value).toBe("ready");
	});

	it("coalesces concurrent opens into one request", async () => {
		let resolve;
		const fetchSchema = vi.fn(() => new Promise((r) => (resolve = r)));
		const { api } = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "s2",
			viewKey: "skills",
			fetchSchema,
		});
		await flushPromises();
		const first = api.requestSchema();
		const second = api.requestSchema();
		await Promise.resolve(); // let the fetcher chain reach the endpoint
		expect(fetchSchema).toHaveBeenCalledTimes(1);
		resolve(SKILLS_SCHEMA);
		await Promise.all([first, second]);
		expect(fetchSchema).toHaveBeenCalledTimes(1);
	});

	it("lets a retry through after a failure", async () => {
		let attempt = 0;
		const fetchSchema = vi.fn(async () => {
			attempt += 1;
			if (attempt === 1) throw envelopeError("list_filter_schema_unavailable", "Down.");
			return SKILLS_SCHEMA;
		});
		const { api } = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "s3",
			viewKey: "skills",
			fetchSchema,
		});
		await flushPromises();
		await api.requestSchema();
		expect(api.filterSchemaState.value).toBe("error");
		await api.requestSchema();
		expect(api.filterSchemaState.value).toBe("ready");
		expect(fetchSchema).toHaveBeenCalledTimes(2);
	});

	it("does nothing at all for a list with no view key", async () => {
		const fetchSchema = vi.fn();
		const { api } = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "s4",
			fetchSchema,
		});
		await flushPromises();
		await api.requestSchema();
		expect(fetchSchema).not.toHaveBeenCalled();
		expect(api.filterState.value.viewKey).toBe("");
	});
});

describe("rolled-back view (the flag contract, M1)", () => {
	it("emits ZERO filters_v2 on the quick-filter path when the view is off (S3/M1.3)", async () => {
		// A declaredRoot lets a quick filter become a v2 clause BEFORE the schema is
		// fetched — the exact path that used to emit without ever consulting the flag.
		// The mount-time capability probe closes it.
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchCapabilities = vi.fn(async () => ({ macros: false }));
		const { api } = host({
			fetchFn,
			storageKey: "cap1",
			viewKey: "macros",
			rootDoctype: "Jarvis Macro",
			quickClauses: { enabled: "enabled" },
			initialFilters: { enabled: "1" },
			fetchCapabilities,
		});
		await flushPromises();
		expect(fetchCapabilities).toHaveBeenCalled();
		expect(fetchFn.mock.calls[0][0].filters_v2).toEqual([]);
		// legacy transport still carries the quick filter, so the list is not broken
		expect(fetchFn.mock.calls[0][0].filters).toEqual({ enabled: "1" });
		// and a RUNTIME quick change still emits no v2
		await api.setFilters({ enabled: "0" });
		expect(fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([]);
	});

	it("does not apply or count a shared link's filters when off, and says so (M1.4)", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [{ name: "a" }], total: 1 }));
		const fetchCapabilities = vi.fn(async () => ({ macros: false }));
		const param = JSON.stringify({
			v: 1,
			k: "macros",
			c: [["Jarvis Macro", "macro_name", "like", "x"]],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const fetchSchema = vi.fn();
		const { api } = host({
			fetchFn,
			storageKey: "cap2",
			viewKey: "macros",
			route,
			router,
			fetchSchema,
			fetchCapabilities,
		});
		await flushPromises();
		// not applied (unfiltered rows), not counted (zero badge), and honestly noticed
		expect(fetchFn.mock.calls[0][0].filters_v2).toEqual([]);
		expect(api.filterState.value.activeCount).toBe(0);
		expect(api.filterState.value.filtersDisabled).toBe(true);
		expect(api.filterNotice.value).toMatch(/turned off for this list/);
		// distinct DISABLED state, and no catalog fetch a retry could chase (UX1)
		expect(api.filterSchemaState.value).toBe("disabled");
		await api.requestSchema();
		expect(fetchSchema).not.toHaveBeenCalled();
	});

	it("renders a disabled view distinctly from a transient schema failure (UX1)", async () => {
		const disabled = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "cap3",
			viewKey: "macros",
			fetchSchema: vi.fn(async () => ({
				ok: false,
				error: { code: "list_filter_view_rolled_back", message: "off" },
			})),
		});
		await flushPromises();
		await disabled.api.requestSchema();
		expect(disabled.api.filterSchemaState.value).toBe("disabled");

		let attempts = 0;
		const transient = host({
			fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
			storageKey: "cap4",
			viewKey: "skills",
			fetchSchema: vi.fn(async () => {
				attempts += 1;
				throw envelopeError("list_filter_schema_unavailable", "Down.");
			}),
		});
		await flushPromises();
		await transient.api.requestSchema();
		expect(transient.api.filterSchemaState.value).toBe("error"); // transient keeps retry
		await transient.api.requestSchema();
		expect(attempts).toBe(2); // a transient failure CAN be retried; disabled cannot
	});

	it("reports bounded AND skipped together, each naming its field (N4/UX2)", async () => {
		const fetchFn = vi.fn(async () => ({ rows: [], total: 0 }));
		const fetchSchema = vi.fn(async () => SKILLS_SCHEMA);
		const big = "x".repeat(1001);
		const param = JSON.stringify({
			v: 1,
			k: "skills",
			c: [
				["Jarvis Custom Skill", "description", "like", "keep"],
				["Jarvis Custom Skill", "description", "like", big], // bounded (too large)
				["Jarvis Custom Skill", "description", "NOPE", "y"], // skipped (bad operator)
			],
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const { api } = host({
			fetchFn,
			storageKey: "n4",
			viewKey: "skills",
			route,
			router,
			fetchSchema,
		});
		await flushPromises();
		const notice = api.filterNotice.value;
		expect(notice).toMatch(/too large to apply/); // bounded reported
		expect(notice).toMatch(/wasn't valid|not valid/); // skipped reported — BOTH, not one
		expect(notice).toMatch(/Description/); // UX2: the field is named
		expect(fetchFn.mock.calls[0][0].filters_v2).toHaveLength(1); // only the good one
	});
});
