import { describe, expect, it } from "vitest";

import { coerceOut, coerceRow, isFieldWritable } from "./draftApply";

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
	it("empty / null cells are skipped", () => {
		const t = { columns: [col(), { fieldname: "d", fieldtype: "Data" }] };
		expect(coerceRow(t, { c: "", d: null }, "create")).toEqual({});
	});
	it("Check: truthy string tokens normalize to 1 (not Number(v) -> 0)", () => {
		const t = { columns: [{ fieldname: "c", fieldtype: "Check", read_only: 0 }] };
		expect(coerceRow(t, { c: "Yes" }, "create")).toEqual({ c: 1 });
		expect(coerceRow(t, { c: "true" }, "create")).toEqual({ c: 1 });
		expect(coerceRow(t, { c: "1" }, "create")).toEqual({ c: 1 });
		expect(coerceRow(t, { c: 1 }, "create")).toEqual({ c: 1 });
		expect(coerceRow(t, { c: "No" }, "create")).toEqual({ c: 0 });
	});
	it("numeric fieldtypes coerce to Number", () => {
		const t = { columns: [{ fieldname: "n", fieldtype: "Float", read_only: 0 }] };
		expect(coerceRow(t, { n: "3.5" }, "create")).toEqual({ n: 3.5 });
	});
});
