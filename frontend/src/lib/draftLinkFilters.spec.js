import { describe, expect, it } from "vitest";
import { draftLinkSearch, resolveLinkFilters } from "./draftLinkFilters";
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
					rows: [{ __name: "row-1", credit_to: "Existing account", currency: "INR" }],
				},
			],
		};
		const before = JSON.stringify(model);
		expect(draftLinkSearch(model, "t:0:0:credit_to")).toEqual({
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
	it("leaves unconstrained and independently configured fields unconstrained", () => {
		expect(resolveLinkFilters(account(), {})).toEqual([]);
	});
});
