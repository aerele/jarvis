import assert from "node:assert/strict";
import { test } from "node:test";

import { cardTableRemovals, cardTableSummary, cardTableValues } from "./cardTables.js";

const meta = { tables: { items: {}, taxes: {} } };

test("the agent's proposed rows are applied, row names included", () => {
	const action = {
		tables: [
			{
				fieldname: "items",
				rows: [
					{ name: "row-1", qty: 5 },
					{ item_code: "NEW", qty: 1 },
				],
			},
		],
	};
	assert.deepEqual(cardTableValues(action, meta), {
		items: [
			{ name: "row-1", qty: 5 },
			{ item_code: "NEW", qty: 1 },
		],
	});
});

test("no tables -> nothing", () => {
	assert.deepEqual(cardTableValues({}, meta), {});
	assert.deepEqual(cardTableValues({ tables: [] }, meta), {});
});

test("a table the doctype does not have is reported, never dropped silently", () => {
	assert.throws(
		() => cardTableValues({ tables: [{ fieldname: "nope", rows: [] }] }, meta),
		/nope/
	);
});

test("each proposed table gets a summary line", () => {
	const tables = [
		{ fieldname: "items", label: "Items", rows: [{ name: "row-1" }, {}] },
		{ fieldname: "taxes", rows: [{}] },
	];
	assert.deepEqual(cardTableSummary({ verb: "update", tables }), [
		{ label: "Items", value: "2 rows" },
		{ label: "taxes", value: "1 row" },
	]);
	assert.deepEqual(cardTableSummary({}), []);
});

test("an update that would drop saved rows the card never showed is reported", () => {
	const saved = {
		items: [{ name: "r1" }, { name: "r2" }, { name: "r3" }],
		taxes: [{ name: "t1" }],
	};
	const action = {
		tables: [
			{ fieldname: "items", label: "Items", rows: [{ name: "r3", qty: 5 }] },
			{ fieldname: "taxes", rows: [{ name: "t1" }, { account: "New" }] },
		],
	};
	assert.deepEqual(cardTableRemovals(action, saved), ["2 saved rows in Items"]);
	assert.deepEqual(cardTableRemovals({ tables: [{ fieldname: "items", rows: [] }] }, saved), [
		"3 saved rows in items",
	]);
});

test("keeping every saved row (or a table with none) removes nothing", () => {
	const action = { tables: [{ fieldname: "items", rows: [{ name: "r1" }, { qty: 1 }] }] };
	assert.deepEqual(cardTableRemovals(action, { items: [{ name: "r1" }] }), []);
	assert.deepEqual(cardTableRemovals(action, {}), []);
	assert.deepEqual(cardTableRemovals({}, { items: [{ name: "r1" }] }), []);
});

test("rows that are not a list are refused with a readable message", () => {
	const bad = { tables: [{ fieldname: "items", rows: { qty: 1 } }] };
	assert.throws(() => cardTableValues(bad, meta), /items/);
	assert.deepEqual(cardTableSummary(bad), [{ label: "items", value: "0 rows" }]);
	assert.deepEqual(cardTableRemovals(bad, { items: [{ name: "r1" }] }), [
		"1 saved row in items",
	]);
});
