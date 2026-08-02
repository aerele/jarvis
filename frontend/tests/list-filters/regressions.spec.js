import { describe, it, expect, vi, beforeEach } from "vitest";
import { defineComponent, reactive } from "vue";
import { mount, flushPromises } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { SKILLS_SCHEMA } from "./fixtures.js";
import { frappeUiStubs } from "./stubs.js";

const toastDouble = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), create: vi.fn() }));
vi.mock("frappe-ui", () => ({ ...frappeUiStubs(), toast: toastDouble }));
vi.mock("@/api", () => ({ getListFilterSchema: vi.fn(), searchLink: vi.fn() }));
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (key, value) => ref(value) };
});

import { useListPage } from "@/composables/useListPage";
import FilterGroup from "@/components/list/FilterGroup.vue";
import {
	makeClause,
	serializeClauses,
	schemaIndex,
	URL_PARAM,
	MAX_URL_CHARS,
} from "@/components/list/filterModel";

/**
 * The adversarial probe's scenarios, kept as regressions.
 *
 * Two of them (P1-1, P1-2) were real defects: a tab switch silently threw the
 * whole filter set away, and a link the app itself wrote came back as an
 * unfiltered list with nothing said. Both are asserted here in the probe's own
 * shape — a router double whose `push` resolves a named/hash location WITHOUT
 * the current query, exactly as vue-router does.
 */
const ROOT = "Jarvis Custom Skill";
const INDEX = schemaIndex(SKILLS_SCHEMA);

/** `push` drops the query unless the caller carries it — vue-router's real behaviour. */
function routerDouble(initialQuery = {}) {
	const route = reactive({ query: { ...initialQuery } });
	const set = (q) => {
		Object.keys(route.query).forEach((k) => delete route.query[k]);
		Object.assign(route.query, q || {});
	};
	const router = {
		replace: vi.fn(({ query }) => {
			set(query);
			return Promise.resolve();
		}),
		push: vi.fn((to) => {
			set((to && to.query) || {});
			return Promise.resolve();
		}),
	};
	return { route, router, set };
}

function host(options) {
	let api = null;
	const Host = defineComponent({
		setup() {
			api = useListPage(options);
			return () => null;
		},
	});
	mount(Host);
	return api;
}

const base = (extra = {}) => ({
	fetchFn: vi.fn(async () => ({ rows: [], total: 0 })),
	fetchSchema: vi.fn(async () => SKILLS_SCHEMA),
	storageKey: "reg",
	viewKey: "skills",
	rootDoctype: ROOT,
	...extra,
});

beforeEach(() => vi.clearAllMocks());

// ── P1-1 ────────────────────────────────────────────────────────────────────
describe("P1-1 tab switches must not destroy the filter set", () => {
	// Probe D1: set two clauses + a quick filter, switch to Runs, come back.
	it("survives a round trip through a sibling tab, with no wasted fetches", async () => {
		const opts = base({ storageKey: "d1", quickClauses: { enabled: "enabled" } });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		await api.requestSchema();
		await api.setFilter("enabled", "1");
		await api.setClauses([
			...api.filterClauses.value,
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "invoice" }),
		]);
		await flushPromises();
		expect(api.filterClauses.value).toHaveLength(2);
		const paramBefore = route.query[URL_PARAM];
		const fetchesBefore = opts.fetchFn.mock.calls.length;

		// The tab switch, as the page now performs it: query carried across.
		router.push({ name: "MacroRuns", query: { ...route.query } });
		await flushPromises();
		router.push({ name: "MacrosList", query: { ...route.query } });
		await flushPromises();

		expect(api.filterClauses.value).toHaveLength(2);
		expect(api.filters.enabled).toBe("1");
		expect(route.query[URL_PARAM]).toBe(paramBefore);
		expect(opts.fetchFn.mock.calls.length).toBe(fetchesBefore);
	});

	// The belt to that braces: even a navigation that DID drop the query cannot
	// wipe a live filter set, because an authored clear goes through setClauses.
	it("re-asserts its param when a navigation drops the query outright", async () => {
		const opts = base({ storageKey: "d1b" });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		await api.setClauses([
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "invoice" }),
		]);
		await flushPromises();
		const fetches = opts.fetchFn.mock.calls.length;

		router.push({ name: "MacroRuns" }); // the OLD, query-dropping form
		await flushPromises();

		expect(api.filterClauses.value).toHaveLength(1);
		expect(route.query[URL_PARAM]).toBeTruthy();
		expect(opts.fetchFn.mock.calls.length).toBe(fetches);
	});

	// Source pin: the defect was one missing `query` on each navigation, and it
	// is invisible in any test that does not mount the whole page.
	it("every tab navigation on both pilots carries the query", () => {
		const root = resolve(__dirname, "../..");
		const read = (p) => readFileSync(resolve(root, p), "utf8");
		const macros = read("src/pages/macros/MacrosList.vue");
		expect(macros).toMatch(/name: v === "runs" \? "MacroRuns" : "MacrosList",\s*\n\s*query: \{ \.\.\.route\.query \}/);

		// Every hash navigation on the Skills shell — the tab strip plus the three
		// legacy-hash redirects — resolves without the current query unless it
		// carries one.
		const navs = read("src/pages/skills/SkillsPage.vue")
			.split("\n")
			.filter((line) => /router\.(push|replace)\(\{.*hash:/.test(line));
		expect(navs.length).toBeGreaterThanOrEqual(4);
		for (const nav of navs) expect(nav).toContain("query: { ...route.query }");
	});
});

// ── P1-2 ────────────────────────────────────────────────────────────────────
describe("P1-2 an unreadable link is never a silently unfiltered list", () => {
	// Probe A1: nine `like` clauses of 1000 chars each — a payload the APP
	// produced, over the URL bound, which came back empty and silent.
	it("says so when a payload is past the length bound", async () => {
		const long = "x".repeat(1000);
		const clauses = Array.from({ length: 9 }, () =>
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: long })
		);
		const param = serializeClauses("skills", clauses, INDEX);
		expect(param.length).toBeGreaterThan(MAX_URL_CHARS);

		const opts = base({ storageKey: "a1" });
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const api = host({ ...opts, route, router });
		await flushPromises();

		expect(opts.fetchFn.mock.calls[0][0].filters_v2).toEqual([]);
		expect(api.filterNotice.value).toBe(
			"The filters in this link couldn't be read, so this list is unfiltered."
		);
	});

	// Probe A2: a link truncated by whatever carried it.
	it("says so when a shared link arrives truncated", async () => {
		const param = serializeClauses(
			"skills",
			[makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "invoice" })],
			INDEX
		);
		const opts = base({ storageKey: "a2" });
		const { route, router } = routerDouble({ [URL_PARAM]: param.slice(0, -3) });
		const api = host({ ...opts, route, router });
		await flushPromises();

		expect(opts.fetchFn.mock.calls[0][0].filters_v2).toEqual([]);
		expect(api.filterNotice.value).toMatch(/couldn't be read/);
	});

	// The other half: never WRITE a URL that would come back unreadable.
	it("keeps the last shareable link rather than writing one that cannot be read", async () => {
		const opts = base({ storageKey: "a3" });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();

		const small = [
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "invoice" }),
		];
		await api.setClauses(small);
		const shareable = route.query[URL_PARAM];
		expect(shareable).toBeTruthy();

		const huge = Array.from({ length: 9 }, () =>
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "x".repeat(1000) })
		);
		await api.setClauses(huge);
		await flushPromises();

		// the filter itself still applies...
		expect(opts.fetchFn.mock.calls.at(-1)[0].filters_v2).toHaveLength(9);
		// ...the URL keeps the last one that survives a round trip...
		expect(route.query[URL_PARAM]).toBe(shareable);
		// ...and the user is told why.
		expect(api.filterNotice.value).toMatch(/too large to share as a link/);
	});

	it("reports rows inside a readable payload that were not clauses", async () => {
		const opts = base({ storageKey: "a4" });
		const { route, router } = routerDouble({
			[URL_PARAM]: JSON.stringify({
				v: 1,
				k: "skills",
				c: [
					[ROOT, "description", "like", "keep"],
					[ROOT, "description", "NOT-AN-OPERATOR", "x"],
				],
			}),
		});
		const api = host({ ...opts, route, router });
		await flushPromises();
		expect(opts.fetchFn.mock.calls[0][0].filters_v2).toHaveLength(1);
		expect(api.filterNotice.value).toMatch(/1 filter in this link was not valid/);
	});
});

// ── P2 batch ────────────────────────────────────────────────────────────────
describe("P2-1 back/forward gets the same grace as the first load", () => {
	it("fetches the catalog for a navigation that arrives before the panel opened", async () => {
		const opts = base({ storageKey: "p21" });
		const { route, router, set } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		expect(opts.fetchSchema).not.toHaveBeenCalled();

		set({
			[URL_PARAM]: JSON.stringify({
				v: 1,
				k: "skills",
				c: [
					[ROOT, "description", "like", "keep"],
					[ROOT, "skill_bundle", "like", "secret"], // permlevel-1: not in the catalog
				],
			}),
		});
		await flushPromises();

		expect(opts.fetchSchema).toHaveBeenCalledTimes(1);
		expect(api.filterClauses.value).toHaveLength(1);
		expect(api.filterNotice.value).toMatch(/no longer available/);
		expect(opts.fetchFn.mock.calls.at(-1)[0].filters_v2).toHaveLength(1);
	});
});

describe("P2-2 a quick filter is shareable before the panel is ever opened", () => {
	it("canonicalizes and writes the URL with no catalog in hand", async () => {
		const opts = base({ storageKey: "p22", quickClauses: { enabled: "enabled" } });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		expect(opts.fetchSchema).not.toHaveBeenCalled();

		await api.setFilter("enabled", "1");
		await flushPromises();

		expect(api.filterClauses.value).toHaveLength(1);
		expect(JSON.parse(route.query[URL_PARAM])).toEqual({
			v: 1,
			k: "skills",
			c: [[ROOT, "enabled", "=", "1"]],
		});
		expect(opts.fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([
			{ doctype: ROOT, fieldname: "enabled", operator: "=", value: "1" },
		]);
	});

	it("adopts an initialFilters quick filter on mount, URL and all", async () => {
		const opts = base({
			storageKey: "p22b",
			quickClauses: { enabled: "enabled" },
			initialFilters: { enabled: "1", scope: "mine" },
		});
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		expect(api.filterClauses.value).toHaveLength(1);
		expect(route.query[URL_PARAM]).toBeTruthy();
		expect(api.filters.scope).toBe("mine"); // the pseudo-filter stays legacy
	});
});

describe("P2-3 typing is one request, not one per keystroke", () => {
	it("debounces a typed value and commits a discrete pick at once", async () => {
		vi.useFakeTimers();
		const opts = base({ storageKey: "p23", clauseDebounceMs: 300 });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await vi.advanceTimersByTimeAsync(0);
		const before = opts.fetchFn.mock.calls.length;
		const writes = router.replace.mock.calls.length;

		for (const v of ["1", "12", "123", "1234", "12345"]) {
			api.setClauses(
				[makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: v })],
				{ immediate: false }
			);
		}
		// the MODEL is live immediately - only the request waits
		expect(api.filterClauses.value[0].value).toBe("12345");
		expect(opts.fetchFn.mock.calls.length).toBe(before);
		await vi.advanceTimersByTimeAsync(300);
		expect(opts.fetchFn.mock.calls.length).toBe(before + 1);
		expect(router.replace.mock.calls.length).toBe(writes + 1);

		// a discrete pick does not wait
		api.setClauses([makeClause({ doctype: ROOT, fieldname: "enabled", operator: "=", value: "1" })]);
		expect(opts.fetchFn.mock.calls.length).toBe(before + 2);
		vi.useRealTimers();
	});
});

describe("P2-4 a URL-seeded mount keeps the page's own quick filters", () => {
	// Probe B1: mounting a shared link used to DELETE initialFilters.enabled,
	// because reflectQuick ran before anything had adopted it.
	it("adopts before it reflects", async () => {
		const param = serializeClauses(
			"skills",
			[makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "q" })],
			INDEX
		);
		const opts = base({
			storageKey: "b1",
			quickClauses: { enabled: "enabled" },
			initialFilters: { enabled: "1" },
		});
		const { route, router } = routerDouble({ [URL_PARAM]: param });
		const api = host({ ...opts, route, router });
		await flushPromises();

		expect(api.filters.enabled).toBe("1");
		const sent = opts.fetchFn.mock.calls.at(-1)[0];
		expect(sent.filters_v2.map((c) => c.fieldname).sort()).toEqual(["description", "enabled"]);
	});
});

describe("C2 a sibling tab's payload is left alone", () => {
	it("neither applies nor deletes a foreign fv2", async () => {
		const opts = base({ storageKey: "c2" });
		const { route, router, set } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		await api.setClauses([
			makeClause({ doctype: ROOT, fieldname: "description", operator: "like", value: "keep" }),
		]);
		await flushPromises();
		const fetches = opts.fetchFn.mock.calls.length;

		const foreign = JSON.stringify({ v: 1, k: "learning", c: [["X", "y", "=", "z"]] });
		set({ [URL_PARAM]: foreign });
		await flushPromises();

		expect(api.filterClauses.value).toHaveLength(1); // ours survives
		expect(route.query[URL_PARAM]).toBe(foreign); // theirs survives
		expect(opts.fetchFn.mock.calls.length).toBe(fetches);
	});
});

describe("C3 a schema-kind rejection loop terminates", () => {
	it("stops after the catalog says the field is gone", async () => {
		let calls = 0;
		const fetchFn = vi.fn(async () => {
			calls += 1;
			if (calls > 20) throw new Error("RUNAWAY");
			if (calls === 1) return { rows: [], total: 0 };
			const e = new Error("rejected");
			e.messages = [{ ok: false, error: { code: "list_filter_unknown_field", message: "gone" } }];
			throw e;
		});
		const opts = base({ storageKey: "c3", fetchFn });
		const { route, router } = routerDouble();
		const api = host({ ...opts, route, router });
		await flushPromises();
		await api.requestSchema();
		await api.setClauses([makeClause({ doctype: ROOT, fieldname: "ghost", operator: "=", value: "1" })]);
		await flushPromises();
		await flushPromises();

		expect(calls).toBeLessThan(6);
		expect(api.filterClauses.value).toEqual([]);
		expect(api.filterNotice.value).toMatch(/no longer available/);
	});
});

// ── the whole chain, not one link of it ─────────────────────────────────────
describe("C1 typing through the REAL panel is one request", () => {
	// The debounce only counts if it survives every hop between the input and
	// the endpoint: FilterValueControl → FilterGroup → ListPage → the page's
	// setClauses. Each hop has its own test; this one drives all of them at once
	// so a dropped second argument anywhere shows up here.
	it("five keystrokes in the panel produce one fetch and one URL write", async () => {
		vi.useFakeTimers();
		const opts = base({ storageKey: "c1", clauseDebounceMs: 300 });
		const { route, router } = routerDouble();
		let api = null;
		const Host = defineComponent({
			components: { FilterGroup },
			setup() {
				api = useListPage({ ...opts, route, router });
				return { api };
			},
			template: `<FilterGroup
				:schema="api.filterState.value.schema"
				:schema-state="api.filterState.value.schemaState"
				:clauses="api.filterState.value.clauses"
				@update:clauses="api.setClauses"
			/>`,
		});
		const w = mount(Host);
		await vi.advanceTimersByTimeAsync(0);
		await api.requestSchema();
		await vi.advanceTimersByTimeAsync(0);

		// add a Description row through the picker, then type into it
		w.findAllComponents({ name: "Autocomplete" })
			.at(-1)
			.vm.$emit("update:modelValue", { value: `${ROOT}␟description` });
		await vi.advanceTimersByTimeAsync(0);

		const fetchesBefore = opts.fetchFn.mock.calls.length;
		const writesBefore = router.replace.mock.calls.length;
		const input = w.find('input[type="text"]');
		for (const v of ["m", "mo", "mon", "mont", "month"]) {
			await input.setValue(v);
			await vi.advanceTimersByTimeAsync(20);
		}
		// nothing has gone out yet, but the model is already current
		expect(opts.fetchFn.mock.calls.length).toBe(fetchesBefore);
		expect(api.filterClauses.value[0].value).toBe("month");

		await vi.advanceTimersByTimeAsync(300);
		expect(opts.fetchFn.mock.calls.length).toBe(fetchesBefore + 1);
		expect(router.replace.mock.calls.length).toBe(writesBefore + 1);
		expect(opts.fetchFn.mock.calls.at(-1)[0].filters_v2).toEqual([
			{ doctype: ROOT, fieldname: "description", operator: "like", value: "month" },
		]);
		vi.useRealTimers();
	});
});
