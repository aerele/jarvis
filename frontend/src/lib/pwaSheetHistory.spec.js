// The phone's sheet history marker (pwa/src/lib/sheetHistory.js) behind a REAL memory-history router:
// `run()` must end with ITS OWN navigation, not with whichever navigation finishes first.
import { describe, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";
import { makeSheetHistory, SHEET_HASH } from "../../../pwa/src/lib/sheetHistory.js";

const stub = { template: "<div />" };
async function startAt(path, guard) {
	const router = createRouter({
		history: createMemoryHistory(),
		routes: [
			{ path: "/chat/:id", component: stub },
			{ path: "/list", component: stub },
		],
	});
	if (guard) router.beforeEach(guard);
	await router.push(path);
	await router.isReady();
	return router;
}

describe("sheet history run(): tied to its own navigation", () => {
	// Break: a refused or cancelled own navigation never ends the run early or twice, and `own()`
	// goes back to false (a stuck `running` lets every later guard decision through for ever).
	it("a refused back ends the run once and releases own()", async () => {
		let refuse = false;
		const router = await startAt("/chat/a", () => (refuse ? false : undefined));
		const sh = makeSheetHistory(router);
		await sh.mark();
		expect(router.currentRoute.value.hash).toBe(SHEET_HASH);
		refuse = true;
		await sh.release();
		expect(sh.own()).toBe(false);
		expect(router.currentRoute.value.hash).toBe(SHEET_HASH);
	});

	// Break: a push that lands on the current location fails (duplicated); the run must still end.
	it("a duplicated own navigation (failure) ends the run", async () => {
		const router = await startAt("/chat/a");
		const sh = makeSheetHistory(router);
		await router.push({ path: "/chat/a", hash: SHEET_HASH });
		await sh.mark();
		expect(sh.own()).toBe(false);
	});

	// Break: the old run ended on the FIRST afterEach of ANY navigation. Here an unrelated navigation
	// finishes while the marker's back is still pending; the run must keep `own()` true until the
	// back itself reports.
	it("an unrelated navigation finishing mid-run does not end the run", async () => {
		const hooks = [];
		const fake = {
			currentRoute: { value: { path: "/chat/a", query: {}, hash: "" } },
			afterEach: (fn) => {
				hooks.push(fn);
				return () => hooks.splice(hooks.indexOf(fn), 1);
			},
			resolve: (t) => ({ fullPath: `${t.path}${t.hash || ""}` }),
			push: () => new Promise(() => {}),
			back: vi.fn(),
		};
		const sh = makeSheetHistory(fake);
		const pending = sh.mark();
		const emit = (to, from, failure) => [...hooks].forEach((h) => h(to, from, failure));
		const at = (path, hash = "") => ({ path, hash, fullPath: `${path}${hash}` });
		emit(at("/list"), at("/chat/a"));
		expect(sh.own()).toBe(true);
		emit(at("/chat/a", SHEET_HASH), at("/chat/a"));
		await pending;
		expect(sh.own()).toBe(false);
		expect(hooks.length).toBe(0);
	});

	// Break: the same for the pop; an unrelated arrival must not count as the marker's pop.
	it("a pop ends only when it leaves the marker entry", async () => {
		const hooks = [];
		const fake = {
			currentRoute: { value: { path: "/chat/a", query: {}, hash: SHEET_HASH } },
			afterEach: (fn) => {
				hooks.push(fn);
				return () => hooks.splice(hooks.indexOf(fn), 1);
			},
			resolve: (t) => ({ fullPath: `${t.path}${t.hash || ""}` }),
			push: () => Promise.resolve(),
			back: vi.fn(),
		};
		const sh = makeSheetHistory(fake);
		await sh.mark();
		const pending = sh.release();
		const at = (path, hash = "") => ({ path, hash, fullPath: `${path}${hash}` });
		hooks.forEach((h) => h(at("/list"), at("/list")));
		expect(sh.own()).toBe(true);
		hooks.forEach((h) => h(at("/chat/a"), at("/chat/a", SHEET_HASH)));
		await pending;
		expect(sh.own()).toBe(false);
	});

	// Break: open -> close -> Back no longer leaves one history entry per sheet.
	it("open, close, Back: the marker entry is added and removed, then one Back leaves", async () => {
		const router = await startAt("/list");
		await router.push("/chat/a");
		const sh = makeSheetHistory(router);
		await sh.mark();
		expect(router.currentRoute.value.hash).toBe(SHEET_HASH);
		await sh.release();
		expect(router.currentRoute.value.fullPath).toBe("/chat/a");
		router.back();
		await new Promise((r) => setTimeout(r, 20));
		expect(router.currentRoute.value.fullPath).toBe("/list");
	});
});
