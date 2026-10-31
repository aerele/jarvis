import { test } from "node:test";
import assert from "node:assert/strict";
import { WORN_RETRY_MS, createWornLoader } from "./holidayWornLoader.js";

const art = () => ({ installWornCss() {} });

test("load: resolves to the module, installs its css once, and keeps it", async () => {
	let installs = 0;
	let imports = 0;
	const mod = { installWornCss: () => installs++ };
	const loader = createWornLoader({ load: async () => (imports++, mod), report: () => {} });
	assert.equal(loader.loaded(), null);
	assert.equal(await loader.load(), mod);
	assert.equal(await loader.load(), mod);
	assert.equal(loader.loaded(), mod);
	assert.deepEqual([installs, imports], [1, 1]);
});

test("load: a call while the import is pending shares it", async () => {
	let imports = 0;
	const loader = createWornLoader({ load: async () => (imports++, art()), report: () => {} });
	const [a, b] = await Promise.all([loader.load(), loader.load()]);
	assert.equal(a, b);
	assert.equal(imports, 1);
});

test("load: a failure gives null, never rejects, reports once and waits a minute", async () => {
	let clock = 1000;
	let imports = 0;
	const reports = [];
	const loader = createWornLoader({
		load: async () => {
			imports++;
			throw new Error("Failed to fetch dynamically imported module: /x/holiday-worn.js");
		},
		report: (ctx) => reports.push(ctx),
		now: () => clock,
	});
	assert.equal(await loader.load(), null);
	assert.equal(await loader.load(), null); // inside the quiet minute: no new attempt
	assert.equal(imports, 1);
	clock += WORN_RETRY_MS;
	assert.equal(await loader.load(), null);
	assert.equal(imports, 2);
	assert.equal(reports.length, 1); // once per session
	assert.equal(reports[0].error_code, "holiday_art");
	// Fixed text: the reporter drops chunk-load messages, and a path must not leak.
	assert.doesNotMatch(reports[0].message, /holiday-worn|Failed to fetch/);
});

test("load: after a failure it can still load once the minute has passed", async () => {
	let clock = 0;
	let fail = true;
	const mod = art();
	const loader = createWornLoader({
		load: async () => {
			if (fail) throw new Error("offline");
			return mod;
		},
		report: () => {},
		now: () => clock,
	});
	assert.equal(await loader.load(), null);
	fail = false;
	clock = WORN_RETRY_MS;
	assert.equal(await loader.load(), mod);
});

test("load: a css install that throws counts as a failed load", async () => {
	const loader = createWornLoader({
		load: async () => ({
			installWornCss() {
				throw new Error("no document");
			},
		}),
		report: () => {},
	});
	assert.equal(await loader.load(), null);
	assert.equal(loader.loaded(), null);
});
