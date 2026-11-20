import { describe, expect, it } from "vitest";
import {
	DraftLinkTargetError,
	draftLinkContext,
	draftLinkSearch,
	resolveLinkFilters,
} from "./draftLinkFilters";
import { panelField } from "./docFields";

const companyFilter = [["Account", "company", "=", "eval:doc.company"]];
const account = (extra = {}) => ({
	fieldname: "credit_to",
	fieldtype: "Link",
	options: "Account",
	...extra,
});

describe("draft Link field filters", () => {
	it("retains configured filters through the panel model and resolves current values", () => {
		const f = panelField(
			account({ link_filters: JSON.stringify(companyFilter) }),
			"Existing account"
		);
		expect(resolveLinkFilters(f, { company: "Company A" })).toEqual([
			["Account", "company", "=", "Company A"],
		]);
		expect(resolveLinkFilters(f, { company: "Company B" })).toEqual([
			["Account", "company", "=", "Company B"],
		]);
		expect(f.value).toBe("Existing account");
	});
	it("applies configured overrides after the supported Desk query", () => {
		expect(
			resolveLinkFilters(
				account({
					link_query_filters: [...companyFilter, ["Account", "is_group", "=", 0]],
					link_filters: JSON.stringify([
						["Account", "company", "=", "Configured company"],
					]),
				}),
				{}
			)
		).toEqual([
			["Account", "company", "=", "Configured company"],
			["Account", "is_group", "=", 0],
		]);
	});
	it("supports row and parent references, static lists, and zero values", () => {
		const f = account({
			link_filters: [
				["Account", "company", "=", "eval:parent.company"],
				["Account", "account_currency", "=", "eval:doc.currency"],
				["Account", "is_group", "=", "eval:doc.is_group"],
				["Account", "account_type", "in", ["Payable", "Receivable"]],
			],
		});
		expect(
			resolveLinkFilters(f, { currency: "INR", is_group: 0 }, { company: "Company A" })
		).toEqual([
			["Account", "company", "=", "Company A"],
			["Account", "account_currency", "=", "INR"],
			["Account", "is_group", "=", 0],
			["Account", "account_type", "in", ["Payable", "Receivable"]],
		]);
	});
	it.each([{}, { company: "" }, { company: null }])(
		"blocks missing dependencies instead of broadening search: %s",
		(doc) => {
			expect(() =>
				resolveLinkFilters(account({ link_filters: companyFilter }), doc)
			).toThrow("Set Company");
		}
	);
	it.each([
		"not JSON",
		{},
		[["Account", "company"]],
		[["Account", "company", "=", "eval:fetch('/write')"]],
		[["Account", "company", "=", "eval:doc.constructor"]],
		[["Account", "company", "=", "eval:doc.company || 'All'"]],
	])("refuses unresolved or executable filters: %s", (link_filters) => {
		expect(() => resolveLinkFilters(account({ link_filters }), {})).toThrow("need ERPNext");
	});
	it("uses loaded parent values and current header / row edits without changing associations", () => {
		const model = {
			doctype: "Custom Order",
			docName: "ORDER-1",
			linkContext: { company: "Company A", currency: "USD" },
			fields: [
				panelField(
					{ fieldname: "company", fieldtype: "Link", options: "Company" },
					"Company B"
				),
			],
			tables: [
				{
					fieldname: "lines",
					child: "Custom Row",
					columns: [
						account({
							link_filters: [
								["Account", "company", "=", "eval:parent.company"],
								["Account", "account_currency", "=", "eval:doc.currency"],
							],
						}),
					],
					rows: [
						{
							__key: 11,
							__name: "row-1",
							credit_to: "Existing account",
							currency: "INR",
						},
					],
				},
			],
		};
		const before = JSON.stringify(model);
		expect(draftLinkSearch(model, "t:0:11:credit_to")).toEqual({
			doctype: "Account",
			referenceDoctype: "Custom Order",
			linkFieldname: "credit_to",
			filters: [
				["Account", "company", "=", "Company B"],
				["Account", "account_currency", "=", "INR"],
			],
		});
		expect(JSON.stringify(model)).toBe(before);
	});
	it("searches a Link field shown only in the row's full form with its own filters", () => {
		const model = {
			doctype: "Custom Order",
			docName: "",
			linkContext: { company: "Company B" },
			fields: [],
			tables: [
				{
					fieldname: "lines",
					child: "Custom Row",
					columns: [],
					formFields: [
						{ fieldname: "", fieldtype: "Section Break", label: "Accounts" },
						account({
							link_filters: [["Account", "company", "=", "eval:parent.company"]],
						}),
					],
					rows: [{ __key: 11, credit_to: "" }],
				},
			],
		};
		expect(draftLinkSearch(model, "t:0:11:credit_to").filters).toEqual([
			["Account", "company", "=", "Company B"],
		]);
	});
	it("leaves unconstrained and independently configured fields unconstrained", () => {
		expect(resolveLinkFilters(account(), {})).toEqual([]);
	});
});

// a link search is keyed by the row's __key, not its position.
describe("a Dynamic Link searches the DocType its controlling field holds", () => {
	const name = {
		fieldname: "reference_name",
		label: "Name",
		fieldtype: "Dynamic Link",
		options: "reference_doctype",
		link_query_filters: [
			["Sales Invoice", "docstatus", "=", 1],
			["Sales Invoice", "company", "=", "eval:parent.company"],
			["Sales Invoice", "customer", "=", "eval:parent.party"],
			["Journal Entry", "docstatus", "=", 1],
			["Journal Entry", "company", "=", "eval:parent.company"],
		],
	};
	const model = (type) => ({
		doctype: "Payment Entry",
		docName: "",
		linkContext: {},
		fields: [
			{ fieldname: "company", fieldtype: "Link", options: "Company", value: "Acme" },
			{
				fieldname: "party",
				fieldtype: "Dynamic Link",
				options: "party_type",
				value: "Cust A",
			},
		],
		tables: [
			{
				fieldname: "references",
				child: "Payment Entry Reference",
				columns: [
					{
						fieldname: "reference_doctype",
						label: "Type",
						fieldtype: "Link",
						options: "DocType",
					},
					name,
				],
				rows: [{ __key: 1, reference_doctype: type, reference_name: "" }],
			},
		],
	});

	it("targets the row's Type and applies only that Type's filters", () => {
		expect(draftLinkSearch(model("Sales Invoice"), "t:0:1:reference_name")).toMatchObject({
			doctype: "Sales Invoice",
			filters: [
				["Sales Invoice", "docstatus", "=", 1],
				["Sales Invoice", "company", "=", "Acme"],
				["Sales Invoice", "customer", "=", "Cust A"],
			],
		});
		expect(draftLinkSearch(model("Journal Entry"), "t:0:1:reference_name")).toMatchObject({
			doctype: "Journal Entry",
			filters: [
				["Journal Entry", "docstatus", "=", 1],
				["Journal Entry", "company", "=", "Acme"],
			],
		});
	});
	it("a Type with no filters of its own searches unfiltered", () => {
		expect(draftLinkSearch(model("Dunning"), "t:0:1:reference_name").filters).toEqual([]);
	});
	it("with no Type set, it names the field to set first", () => {
		expect(() => draftLinkSearch(model(""), "t:0:1:reference_name")).toThrow(
			DraftLinkTargetError
		);
		expect(() => draftLinkSearch(model(""), "t:0:1:reference_name")).toThrow(
			"Set Type before searching this field."
		);
		expect(draftLinkContext(model(""), "t:0:1:reference_name")).toBeNull();
	});
	it("a three-part filter applies to whichever DocType is the target", () => {
		const f = { ...name, link_query_filters: [["docstatus", "=", 1]] };
		expect(resolveLinkFilters(f, {}, null, "Sales Order")).toEqual([
			["Sales Order", "docstatus", "=", 1],
		]);
	});
	it("a plain Link still refuses a filter for another DocType", () => {
		const f = account({ link_query_filters: [["Customer", "name", "=", "x"]] });
		expect(() => resolveLinkFilters(f, {})).toThrow("need ERPNext");
	});
});

describe("a link search finds its row by __key", () => {
	const model = () => ({
		doctype: "Custom Order",
		docName: "",
		linkContext: {},
		fields: [],
		tables: [
			{
				fieldname: "lines",
				child: "Custom Row",
				columns: [
					account({
						link_filters: [["Account", "currency", "=", "eval:doc.currency"]],
					}),
					{ fieldname: "currency", fieldtype: "Data" },
				],
				rows: [
					{ __key: 5, credit_to: "", currency: "USD" },
					{ __key: 6, credit_to: "", currency: "EUR" },
					{ __key: 7, credit_to: "", currency: "GBP" },
				],
			},
		],
	});
	const filtersFor = (m, key) => draftLinkSearch(m, key).filters[0][3];

	it("keeps searching the original row after a row above it is removed", () => {
		const m = model();
		const key = "t:0:6:credit_to"; // row 2, opened before the removal
		expect(filtersFor(m, key)).toBe("EUR");
		m.tables[0].rows.splice(0, 1); // remove row 1
		expect(filtersFor(m, key)).toBe("EUR"); // by position it would now be GBP
		expect(draftLinkContext(m, key)).toBe(
			JSON.stringify(draftLinkSearch(m, "t:0:6:credit_to"))
		);
	});
	it("a removed row's key no longer resolves (and is not another row's)", () => {
		const m = model();
		m.tables[0].rows.splice(1, 1);
		expect(() => draftLinkSearch(m, "t:0:6:credit_to")).toThrow("need ERPNext");
		expect(draftLinkContext(m, "t:0:6:credit_to")).toBeNull();
	});
	it("a row with no __key never matches, not even the key text 'undefined'", () => {
		const m = model();
		delete m.tables[0].rows[1].__key;
		expect(() => draftLinkSearch(m, "t:0:undefined:credit_to")).toThrow("need ERPNext");
		expect(draftLinkContext(m, "t:0:undefined:credit_to")).toBeNull();
	});
	it("the form's copy of a field has its own suffix and resolves the same row", () => {
		const m = model();
		expect(filtersFor(m, "t:0:7:credit_to:form")).toBe("GBP");
	});
});
