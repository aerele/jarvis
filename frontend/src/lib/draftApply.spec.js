import { describe, expect, it } from "vitest";

import {
	checkToYesNo,
	coerceOut,
	coerceRow,
	isFieldMissing,
	isFieldWritable,
	readonlyDisplay,
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
