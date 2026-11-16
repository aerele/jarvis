// draftFromCard turns a server draft card into the action the form's model builder takes.
// The expected actions below are written out by hand from draftCardFixtures.json, never
// computed from the card.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { draftFromCard } from "./draftFromCard.js";

const FIXTURES = JSON.parse(
	fs.readFileSync(path.resolve(__dirname, "draftCardFixtures.json"), "utf8")
);
const card = (id) => FIXTURES.find((f) => f.id === id).card;

const EXPECTED = {
	"create-simple": {
		kind: "doc",
		verb: "create",
		doctype: "ToDo",
		name: "",
		fields: [{ fieldname: "description", label: "description", value: "Call the supplier" }],
		tables: [],
		continue: 0,
		token: "tok-1",
		source: "card",
	},
	"create-with-table": {
		kind: "doc",
		verb: "create",
		doctype: "Web Form",
		name: "",
		fields: [{ fieldname: "title", label: "title", value: "Feedback" }],
		tables: [
			{ fieldname: "web_form_fields", rows: [{ fieldname: "email", fieldtype: "Data" }] },
		],
		continue: 0,
		token: "tok-1",
		source: "card",
	},
	"create-ignored-field": {
		kind: "doc",
		verb: "create",
		doctype: "Timesheet",
		name: "",
		fields: [{ fieldname: "employee", label: "employee", value: "EMP-1" }],
		tables: [],
		continue: 0,
		token: "tok-1",
		source: "card",
	},
	"update-diff": {
		kind: "doc",
		verb: "update",
		doctype: "ToDo",
		name: "TD-0001",
		fields: [{ fieldname: "status", label: "status", value: "Closed" }],
		tables: [],
		continue: 0,
		token: "tok-1",
		source: "card",
	},
	"update-table-rows": {
		kind: "doc",
		verb: "update",
		doctype: "Timesheet",
		name: "TS-0001",
		fields: [],
		tables: [
			{
				fieldname: "time_logs",
				rows: [
					{ name: "tl-1", from_time: "09:00" },
					{ name: "tl-2", from_time: "11:00" },
					{ from_time: "14:00" },
				],
			},
		],
		continue: 0,
		token: "tok-1",
		source: "card",
	},
};

describe("draftFromCard: the fixtures", () => {
	for (const [id, expected] of Object.entries(EXPECTED)) {
		it(`${id}: exact fieldnames, intact tables, named after the draft`, () => {
			expect(draftFromCard(card(id), "tok-1")).toEqual(expected);
		});
	}

	it("a sensitive card and an unreadable card give null", () => {
		expect(draftFromCard(card("sensitive"), "t")).toBeNull();
		expect(draftFromCard(card("unreadable"), "t")).toBeNull();
	});
});

describe("draftFromCard: the shape of the draft", () => {
	const mk = (draft) => ({ kind: "create", draft: { verb: "create", doctype: "X", ...draft } });

	it("keeps the order of the values, with lists as tables and the rest as fields", () => {
		const a = draftFromCard(mk({ values: { b: 1, rows: [{ q: 1 }], a: "x", more: [] } }), "t");
		expect(a.fields.map((f) => f.fieldname)).toEqual(["b", "a"]);
		expect(a.tables.map((t) => t.fieldname)).toEqual(["rows", "more"]);
		expect(a.tables[1].rows).toEqual([]);
	});

	it("a name of null or '' is no name; a prompt-named create keeps its name in values", () => {
		expect(draftFromCard(mk({ name: null, values: { a: 1 } }), "t").name).toBe("");
		expect(draftFromCard(mk({ name: "", values: { a: 1 } }), "t").name).toBe("");
		const named = draftFromCard(mk({ name: "ACME", values: { name: "ACME", a: 1 } }), "t");
		expect(named.name).toBe("ACME");
		expect(named.fields.map((f) => f.fieldname)).toEqual(["name", "a"]);
	});

	it("a row that is not an object keeps its place as an empty row, so _draft_row indexes hold", () => {
		const a = draftFromCard(mk({ values: { rows: [{ q: 1 }, null, "x", { q: 2 }] } }), "t");
		expect(a.tables[0].rows).toEqual([{ q: 1 }, {}, {}, { q: 2 }]);
	});

	it("rows are deep copies, even of a reactive proxy: editing the action never edits the card", () => {
		const c = mk({ values: { rows: [{ q: 1, meta: { tags: ["a"] } }] } });
		const rows = draftFromCard(c, "t").tables[0].rows;
		rows[0].q = 99;
		rows[0].meta.tags.push("b");
		expect(c.draft.values.rows[0]).toEqual({ q: 1, meta: { tags: ["a"] } });
		// a Proxy (Vue's reactive card) makes structuredClone throw: the JSON fallback copies it
		const live = mk({ values: { rows: [new Proxy({ q: 1, meta: { tags: ["a"] } }, {})] } });
		const copy = draftFromCard(live, "t").tables[0].rows[0];
		copy.meta.tags.push("b");
		expect(live.draft.values.rows[0].meta.tags).toEqual(["a"]);
		expect(copy).toEqual({ q: 1, meta: { tags: ["a", "b"] } });
	});

	it("a JSON object value becomes JSON text; null stays null; a number stays a number", () => {
		const a = draftFromCard(mk({ values: { j: { a: 1 }, n: null, q: 5 } }), "t");
		expect(a.fields.map((f) => f.value)).toEqual(['{"a":1}', null, 5]);
	});

	it("continue is 1 or 0; a missing token is ''", () => {
		expect(draftFromCard(mk({ values: { a: 1 }, continue: true }), "t").continue).toBe(1);
		expect(draftFromCard(mk({ values: { a: 1 } }), undefined).token).toBe("");
	});
});

describe("draftFromCard: a card it cannot read is null and never throws", () => {
	const hostile = [
		null,
		undefined,
		"a string",
		7,
		[],
		{},
		{ draft: 5 },
		{ draft: null },
		{ draft: [] },
		{ draft: { verb: "create", doctype: "X", values: [] } },
		{ draft: { verb: "create", doctype: "X", values: null } },
		{ draft: { verb: "delete", doctype: "X", values: {} } },
		{ draft: { verb: "create", doctype: "X", values: {} }, risk: "sensitive" },
		{
			get draft() {
				throw new Error("boom");
			},
		},
	];
	hostile.forEach((c, i) => {
		it(`input ${i}`, () => {
			expect(draftFromCard(c, "t")).toBeNull();
		});
	});
});
