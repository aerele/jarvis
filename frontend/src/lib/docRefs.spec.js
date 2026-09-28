import { describe, it, expect } from "vitest";
import { collectDocRefs } from "./docRefs";

const tool = (args, result) => ({
	role: "tool",
	tool_args: JSON.stringify(args),
	tool_result: JSON.stringify(result),
});

describe("collectDocRefs", () => {
	it("maps a created record to its DocType", () => {
		const refs = collectDocRefs([
			tool(
				{ doctype: "Sales Order", values: {} },
				{ ok: true, data: { doctype: "Sales Order", name: "SAL-ORD-2026-00006" } }
			),
		]);
		expect(refs).toEqual({ "SAL-ORD-2026-00006": "Sales Order" });
	});

	it("skips a DocType record read with get_doc (#620)", () => {
		const refs = collectDocRefs([
			tool(
				{ doctype: "DocType", name: "Sales Order" },
				{ ok: true, data: { doctype: "DocType", name: "Sales Order", module: "Selling" } }
			),
		]);
		expect(refs).toEqual({});
	});

	it("skips a DocType record whose result omits its own doctype key (#620)", () => {
		const refs = collectDocRefs([
			tool({ doctype: "DocType" }, { ok: true, data: { name: "Sales Order" } }),
		]);
		expect(refs).toEqual({});
	});

	it("skips DocType rows from get_list, whose rows carry no doctype key (#620)", () => {
		const refs = collectDocRefs([
			tool(
				{ doctype: "DocType", filters: { module: "Selling" } },
				{ ok: true, data: [{ name: "Quotation" }, { name: "Sales Order" }] }
			),
		]);
		expect(refs).toEqual({});
	});

	it("lets a row's own doctype override the listed doctype", () => {
		const refs = collectDocRefs([
			tool(
				{ doctype: "DocType" },
				{ ok: true, data: [{ doctype: "Customer", name: "Grant Plastics Ltd." }] }
			),
		]);
		expect(refs).toEqual({ "Grant Plastics Ltd.": "Customer" });
	});

	it("ignores names shorter than 4 characters and non-tool rows", () => {
		const refs = collectDocRefs([
			tool({ doctype: "UOM", name: "Nos" }, { ok: true }),
			{ role: "assistant", content: "Created Sales Order SAL-ORD-2026-00006." },
		]);
		expect(refs).toEqual({});
	});

	it("survives unparsable and null tool payloads", () => {
		const refs = collectDocRefs([
			{ role: "tool", tool_args: "{", tool_result: "not json" },
			{ role: "tool", tool_args: "null", tool_result: "null" },
		]);
		expect(refs).toEqual({});
	});
});
