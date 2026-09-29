import { describe, expect, it } from "vitest";

import {
	checkToYesNo,
	coerceOut,
	coerceRow,
	isFieldMissing,
	isFieldWritable,
	isRowKeyColumn,
	overlaySavedRows,
	readonlyDisplay,
	removedSavedRows,
	tableChanged,
	tableRowsPayload,
	toPanelRow,
} from "./draftApply";

describe("isFieldWritable", () => {
	it("create: a read_only field the agent PROPOSED is written", () => {
		expect(isFieldWritable({ read_only: 1, proposed: true }, "create")).toBe(true);
	});
	it("create: an UNPROPOSED read_only field is not written (reqd-Check default clobber)", () => {
		expect(isFieldWritable({ read_only: 1, proposed: false }, "create")).toBe(false);
	});
	it("update: a read_only field is never written, even if proposed", () => {
		expect(isFieldWritable({ read_only: 1, proposed: true }, "update")).toBe(false);
	});
	it("a non-read_only field is always writable", () => {
		expect(isFieldWritable({ read_only: 0, proposed: false }, "create")).toBe(true);
		expect(isFieldWritable({ read_only: 0, proposed: false }, "update")).toBe(true);
	});
});

describe("isFieldMissing", () => {
	it("required + editable + empty -> missing", () => {
		expect(isFieldMissing({ reqd: 1, read_only: 0, value: "" })).toBe(true);
	});
	it("required + read_only + empty -> NOT missing (user can't fill it)", () => {
		expect(isFieldMissing({ reqd: 1, read_only: 1, value: "" })).toBe(false);
	});
	it("required + editable + filled -> not missing", () => {
		expect(isFieldMissing({ reqd: 1, read_only: 0, value: "x" })).toBe(false);
	});
	it("not required -> never missing", () => {
		expect(isFieldMissing({ reqd: 0, read_only: 0, value: "" })).toBe(false);
	});
});

describe("readonlyDisplay", () => {
	it("empty -> em dash", () => {
		expect(readonlyDisplay({ value: "" })).toBe("-");
		expect(readonlyDisplay({ value: null })).toBe("-");
	});
	it("datetime -> T swapped for a space", () => {
		expect(readonlyDisplay({ control: "datetime", value: "2026-08-31T14:30" })).toBe(
			"2026-08-31 14:30"
		);
	});
	it("other controls pass through verbatim", () => {
		expect(readonlyDisplay({ control: "data", value: "hello" })).toBe("hello");
		expect(readonlyDisplay({ control: "date", value: "2026-08-31" })).toBe("2026-08-31");
	});
});

describe("checkToYesNo", () => {
	it("truthy tokens -> Yes", () => {
		for (const v of ["1", 1, "yes", "Yes", "true", true, "on"]) {
			expect(checkToYesNo(v)).toBe("Yes");
		}
	});
	it("everything else -> No", () => {
		for (const v of ["0", 0, "", "no", false, "2"]) {
			expect(checkToYesNo(v)).toBe("No");
		}
	});
});

describe("coerceOut", () => {
	it("check -> 1/0 from the Yes/No control string", () => {
		expect(coerceOut({ control: "check", value: "Yes" })).toBe(1);
		expect(coerceOut({ control: "check", value: "No" })).toBe(0);
	});
	it("number -> Number, empty stays empty", () => {
		expect(coerceOut({ control: "number", value: "5" })).toBe(5);
		expect(coerceOut({ control: "number", value: "" })).toBe("");
	});
	it("other controls pass through", () => {
		expect(coerceOut({ control: "text", value: "hi" })).toBe("hi");
	});
});

describe("coerceRow", () => {
	const col = (o) => ({ fieldname: "c", fieldtype: "Data", read_only: 0, ...o });

	it("create: a read_only child column carrying a value is filled", () => {
		const t = { columns: [col({ read_only: 1 })] };
		expect(coerceRow(t, { c: "v" }, "create")).toEqual({ c: "v" });
	});
	it("update: a read_only child column is stripped", () => {
		const t = { columns: [col({ read_only: 1 })] };
		expect(coerceRow(t, { c: "v" }, "update")).toEqual({});
	});
	it("verb omitted (origJson baseline) strips read_only, same as update", () => {
		const t = { columns: [col({ read_only: 1 })] };
		expect(coerceRow(t, { c: "v" })).toEqual({});
	});
	it("empty / null cells are skipped", () => {
		const t = { columns: [col(), { fieldname: "d", fieldtype: "Data" }] };
		expect(coerceRow(t, { c: "", d: null }, "create")).toEqual({});
	});
	it("Check: truthy tokens normalize to 1 (not Number(v) -> 0)", () => {
		const t = { columns: [{ fieldname: "c", fieldtype: "Check", read_only: 0 }] };
		for (const v of ["Yes", "true", "on", "1", 1, true]) {
			expect(coerceRow(t, { c: v }, "create")).toEqual({ c: 1 });
		}
		expect(coerceRow(t, { c: "No" }, "create")).toEqual({ c: 0 });
	});
	it("numeric fieldtypes coerce to Number", () => {
		for (const ft of ["Int", "Float", "Currency", "Percent"]) {
			const t = { columns: [{ fieldname: "n", fieldtype: ft, read_only: 0 }] };
			expect(coerceRow(t, { n: "3.5" }, "create")).toEqual({ n: 3.5 });
		}
	});
});

describe("child rows keep their name (CR-3: an update edits THAT row)", () => {
	const cols = [
		{ fieldname: "qty", fieldtype: "Float", read_only: 0 },
		{ fieldname: "from_time", fieldtype: "Datetime", read_only: 0 },
	];
	const saved = { name: "row-1", qty: 2, from_time: "2026-09-01 09:30:00", project: "P-1" };

	it("toPanelRow keeps the name out of the columns and normalizes dates", () => {
		const r = toPanelRow(cols, saved);
		expect(r).toEqual({ __name: "row-1", qty: "2", from_time: "2026-09-01T09:30" });
	});
	it("coerceRow sends the row name with the edited values", () => {
		const t = { columns: cols };
		expect(coerceRow(t, { ...toPanelRow(cols, saved), qty: "5" }, "update")).toEqual({
			name: "row-1",
			qty: 5,
			from_time: "2026-09-01T09:30",
		});
	});
	it("a new panel row has no name", () => {
		const t = { columns: cols };
		expect(coerceRow(t, { qty: "1", from_time: "" }, "update")).toEqual({ qty: 1 });
	});
	it("an untouched table with a Datetime column is NOT a change", () => {
		const orig = [saved];
		const t = { columns: cols, rows: orig.map((r) => toPanelRow(cols, r)) };
		expect(tableChanged(t, orig)).toBe(false);
	});
	it("an edited cell is a change", () => {
		const orig = [saved];
		const t = { columns: cols, rows: [{ ...toPanelRow(cols, saved), qty: "3" }] };
		expect(tableChanged(t, orig)).toBe(true);
	});
	it("a removed row is a change", () => {
		const t = { columns: cols, rows: [] };
		expect(tableChanged(t, [saved])).toBe(true);
	});
});

describe("tableRowsPayload: an existing row sends only what changed", () => {
	const cols = [
		{ fieldname: "qty", fieldtype: "Float", read_only: 0 },
		{ fieldname: "description", fieldtype: "Data", read_only: 0 },
		{ fieldname: "from_time", fieldtype: "Datetime", read_only: 0 },
	];
	const saved = { name: "r1", qty: 2, description: "old", from_time: "2026-09-01 09:30:15" };
	const table = (rows) => ({ columns: cols, rows });

	it("an edited cell is sent with the row name, nothing else", () => {
		const t = table([{ ...toPanelRow(cols, saved), qty: "5" }]);
		expect(tableRowsPayload(t, [saved])).toEqual([{ name: "r1", qty: 5 }]);
	});
	it("a cleared cell is sent as null so it really clears", () => {
		const t = table([{ ...toPanelRow(cols, saved), description: "" }]);
		expect(tableRowsPayload(t, [saved])).toEqual([{ name: "r1", description: null }]);
	});
	it("an untouched Datetime keeps its seconds (never resent)", () => {
		const t = table([{ ...toPanelRow(cols, saved), qty: "3" }]);
		expect(tableRowsPayload(t, [saved])[0]).not.toHaveProperty("from_time");
	});
	it("a new row sends its filled cells, no name", () => {
		const t = table([toPanelRow(cols, saved), { qty: "1", description: "", from_time: "" }]);
		expect(tableRowsPayload(t, [saved])).toEqual([{ name: "r1" }, { qty: 1 }]);
	});
	it("a proposed row that carries a name edits that row", () => {
		const proposed = {
			name: "r1",
			qty: 7,
			description: "old",
			from_time: "2026-09-01 09:30:15",
		};
		const t = table([toPanelRow(cols, proposed)]);
		expect(tableRowsPayload(t, [saved])).toEqual([{ name: "r1", qty: 7 }]);
	});
	it("reordered rows are a change", () => {
		const s2 = { name: "r2", qty: 1, description: "x", from_time: "" };
		const t = table([toPanelRow(cols, s2), toPanelRow(cols, saved)]);
		expect(tableChanged(t, [saved, s2])).toBe(true);
	});
	it("an added row is a change", () => {
		const t = table([toPanelRow(cols, saved), { qty: "1", description: "", from_time: "" }]);
		expect(tableChanged(t, [saved])).toBe(true);
	});
});

describe("a proposal that names a kept row with only its changed cells", () => {
	const columns = [
		{ fieldname: "item_code", fieldtype: "Link" },
		{ fieldname: "qty", fieldtype: "Float" },
		{ fieldname: "rate", fieldtype: "Currency" },
	];
	const saved = [
		{ name: "r1", item_code: "A", qty: 2, rate: 10 },
		{ name: "r2", item_code: "B", qty: 1, rate: 5 },
	];

	it("is laid over the saved row, so no column it left out is blanked", () => {
		const proposed = [{ name: "r1" }, { name: "r2", qty: 3 }];
		const table = {
			fieldname: "items",
			columns,
			rows: overlaySavedRows(proposed, saved).map((r) => toPanelRow(columns, r)),
		};
		expect(table.rows[1]).toMatchObject({ __name: "r2", item_code: "B", qty: "3", rate: "5" });
		expect(tableChanged(table, saved)).toBe(true);
		expect(tableRowsPayload(table, saved)).toEqual([{ name: "r1" }, { name: "r2", qty: 3 }]);
	});

	it("keeps a new row and an unknown name as proposed", () => {
		expect(overlaySavedRows([{ item_code: "C" }, { name: "zz", qty: 1 }], saved)).toEqual([
			{ item_code: "C" },
			{ name: "zz", qty: 1 },
		]);
		expect(overlaySavedRows(null, saved)).toEqual([]);
	});

	it("never turns the row id or a __ key into a grid column", () => {
		expect(["name", "__islocal", "qty"].map(isRowKeyColumn)).toEqual([false, false, true]);
	});
});

describe("removedSavedRows", () => {
	const columns = [
		{ fieldname: "ro", fieldtype: "Data", read_only: 1 },
		{ fieldname: "item_code", fieldtype: "Link" },
	];
	const origJson = JSON.stringify([
		{ name: "r1", item_code: "A" },
		{ name: "r2", item_code: "" },
	]);

	it("labels each dropped saved row by its first editable column, else its id", () => {
		expect(removedSavedRows({ columns, rows: [], origJson })).toEqual(["A", "r2"]);
	});

	it("is empty when every saved row is kept, on a create, or on bad origJson", () => {
		const rows = [{ __name: "r1" }, { __name: "r2" }, { item_code: "new" }];
		expect(removedSavedRows({ columns, rows, origJson })).toEqual([]);
		expect(removedSavedRows({ columns, rows: [], origJson: "null" })).toEqual([]);
		expect(removedSavedRows({ columns, rows: [], origJson: "{bad" })).toEqual([]);
	});

	it("system keys are not grid columns either", () => {
		expect(["idx", "parent", "creation", "doctype"].some(isRowKeyColumn)).toBe(false);
	});
});
