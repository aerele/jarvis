import { test } from "node:test";
import assert from "node:assert/strict";
import { createHolidaySwitch } from "./holidaySwitch.js";

function setup(over = {}) {
	const state = { value: true, loaded: false, loadFailed: false, saving: false };
	const shown = [];
	const saved = [];
	const errors = [];
	const sw = createHolidaySwitch({
		state,
		getSettings: async () => ({ ok: true, data: { show_holiday_touches: 0 } }),
		saveSetting: async (v) => saved.push(v),
		setShown: (v) => shown.push(v),
		onError: (e) => errors.push(e),
		...over,
	});
	return { state, shown, saved, errors, sw };
}

test("load: the server value wins, the store follows it, and the row is usable after", async () => {
	const { state, shown, sw } = setup();
	assert.equal(state.loaded, false);
	await sw.load();
	assert.equal(state.value, false);
	assert.deepEqual(shown, [false]);
	assert.equal(state.loaded, true);
});

test("load: a failed read keeps the row disabled and says so, the default stays on", async () => {
	const { state, shown, sw } = setup({
		getSettings: async () => {
			throw new Error("offline");
		},
	});
	await sw.load();
	assert.equal(state.value, true);
	assert.deepEqual(shown, []);
	assert.equal(state.loaded, false);
	assert.equal(state.loadFailed, true);
});

test("load: an ok:false answer counts as a failed read", async () => {
	const { state, sw } = setup({ getSettings: async () => ({ ok: false }) });
	await sw.load();
	assert.equal(state.loaded, false);
	assert.equal(state.loadFailed, true);
});

test("retry: after a failed read the next call loads, enables the row and clears the hint", async () => {
	let fail = true;
	const { state, shown, sw } = setup({
		getSettings: async () => {
			if (fail) throw new Error("offline");
			return { ok: true, data: { show_holiday_touches: 0 } };
		},
	});
	await sw.load();
	fail = false;
	await sw.retry();
	assert.equal(state.loaded, true);
	assert.equal(state.loadFailed, false);
	assert.equal(state.value, false);
	assert.deepEqual(shown, [false]);
});

test("retry: does nothing once loaded, and never overlaps a read in flight", async () => {
	let calls = 0;
	let release;
	const { state, sw } = setup({
		getSettings: () => {
			calls += 1;
			return new Promise(
				(resolve) =>
					(release = () => resolve({ ok: true, data: { show_holiday_touches: 1 } }))
			);
		},
	});
	const first = sw.load();
	sw.retry(); // a read is in flight
	assert.equal(calls, 1);
	release();
	await first;
	await sw.retry(); // already loaded
	assert.equal(calls, 1);
	assert.equal(state.loaded, true);
});

test("load: an answer without the key changes nothing", async () => {
	const { state, shown, sw } = setup({ getSettings: async () => ({ ok: true, data: {} }) });
	await sw.load();
	assert.equal(state.value, true);
	assert.deepEqual(shown, []);
});

test("toggle: saves 1 or 0 first, and tells the store only after the save", async () => {
	const order = [];
	const { state, sw } = setup({
		saveSetting: async (v) => order.push(`save:${v}`),
		setShown: (v) => order.push(`shown:${v}`),
	});
	await sw.toggle(false);
	assert.deepEqual(order, ["save:0", "shown:false"]);
	assert.equal(state.value, false);
	assert.equal(state.saving, false);
});

test("toggle: a failed save puts the switch back, leaves the store alone and reports", async () => {
	const boom = new Error("403");
	const { state, shown, errors, sw } = setup({
		saveSetting: async () => {
			throw boom;
		},
	});
	await sw.toggle(false);
	assert.equal(state.value, true);
	assert.deepEqual(shown, []);
	assert.deepEqual(errors, [boom]);
	assert.equal(state.saving, false);
});

test("toggle: a second tap while saving is ignored", async () => {
	let release;
	const { state, saved, sw } = setup({
		saveSetting: (v) =>
			new Promise((resolve) => {
				saved.push(v);
				release = resolve;
			}),
	});
	const first = sw.toggle(false);
	assert.equal(state.saving, true);
	await sw.toggle(true);
	release();
	await first;
	assert.deepEqual(saved, [0]);
	assert.equal(state.value, false);
});
