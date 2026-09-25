import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/api", () => ({ searchLink: vi.fn(async () => [{ value: "Local" }]) }));

import { searchLink } from "@/api";
import DocFieldsForm from "./DocFieldsForm.vue";

const HOSTILE = '<img src=x onerror="window.__pwned=1">';

const meta = {
	fields: [
		{ fieldname: "supplier_name", label: "Supplier Name", fieldtype: "Data", reqd: 1 },
		{
			fieldname: "supplier_type",
			label: "Supplier Type",
			fieldtype: "Select",
			options: "\nCompany\nIndividual",
			reqd: 1,
		},
		{
			fieldname: "supplier_group",
			label: "Supplier Group",
			fieldtype: "Link",
			options: "Supplier Group",
		},
		{ fieldname: "is_frozen", label: "Frozen", fieldtype: "Check" },
		{ fieldname: "credit_days", label: "Credit Days", fieldtype: "Int" },
		{ fieldname: "tax_id", label: "Tax ID", fieldtype: "Data" },
		{ fieldname: "companies", label: "Companies", fieldtype: "Table" },
	],
	tables: {
		companies: { child_doctype: "Allowed To Transact With", label: "Companies", columns: [] },
	},
};

function mountForm(props = {}) {
	return mount(DocFieldsForm, {
		props: {
			meta,
			values: { supplier_name: "Acme", tax_id: "27ABC", supplier_type: "" },
			needsInput: [{ fieldname: "supplier_type" }],
			locked: { tax_id: "It identifies this record." },
			errors: {},
			...props,
		},
	});
}

const field = (w, label) =>
	w.findAll("label").find((l) => l.text().replace("*", "").trim() === label);
const control = (w, label) => w.find("#" + field(w, label).attributes("for"));

describe("DocFieldsForm", () => {
	beforeEach(() => vi.clearAllMocks());
	afterEach(() => vi.useRealTimers());

	it("shows the proposed fields and the missing one, flagged", () => {
		const w = mountForm();
		expect(field(w, "Supplier Name")).toBeTruthy();
		expect(field(w, "Supplier Group")).toBeFalsy(); // not proposed, not missing
		const type = control(w, "Supplier Type");
		expect(type.element.tagName).toBe("SELECT");
		expect(type.attributes("aria-invalid")).toBe("true");
		const note = w.find("#" + type.attributes("aria-describedby"));
		expect(note.text()).toContain("Required");
	});

	it("emits coerced values", async () => {
		const w = mountForm({
			values: { supplier_name: "Acme", supplier_type: "", is_frozen: 0, credit_days: 5 },
		});
		await control(w, "Supplier Type").setValue("Company");
		expect(w.emitted("update:values").at(-1)[0].supplier_type).toBe("Company");
		await control(w, "Frozen").setValue(true);
		expect(w.emitted("update:values").at(-1)[0].is_frozen).toBe(1);
		await control(w, "Credit Days").setValue("30");
		expect(w.emitted("update:values").at(-1)[0].credit_days).toBe(30);
	});

	it("a filled missing field is no longer flagged", () => {
		const w = mountForm({ values: { supplier_name: "Acme", supplier_type: "Company" } });
		expect(control(w, "Supplier Type").attributes("aria-invalid")).toBe("false");
	});

	it("locked fields are read-only with their reason", () => {
		const w = mountForm();
		const tax = control(w, "Tax ID");
		expect(tax.element.tagName).toBe("DIV");
		expect(tax.text()).toBe("27ABC");
		expect(w.find("#" + tax.attributes("aria-describedby")).text()).toBe(
			"It identifies this record."
		);
	});

	it("shows a server error inline", () => {
		const w = mountForm({ errors: { supplier_name: "Too long" } });
		const name = control(w, "Supplier Name");
		expect(name.attributes("aria-invalid")).toBe("true");
		expect(w.find("#" + name.attributes("aria-describedby")).text()).toBe("Too long");
	});

	it("a table adds the column a missing child field needs, and edits the row", async () => {
		const w = mountForm({
			values: { supplier_name: "Acme", companies: [{}] },
			needsInput: [
				{
					fieldname: "company",
					parentfield: "companies",
					idx: 1,
					field: {
						fieldname: "company",
						label: "Company",
						fieldtype: "Link",
						options: "Company",
					},
				},
			],
		});
		expect(w.find('[role="group"]').attributes("aria-label")).toBe("Companies row 1");
		const company = control(w, "Company");
		expect(company.attributes("aria-invalid")).toBe("true");
		await company.setValue("Acme Co");
		expect(w.emitted("update:values").at(-1)[0].companies).toEqual([{ company: "Acme Co" }]);
	});

	it("suggests link values from search_link", async () => {
		vi.useFakeTimers();
		const w = mountForm({ values: { supplier_name: "Acme", supplier_group: "" } });
		await control(w, "Supplier Group").setValue("Lo");
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(searchLink).toHaveBeenCalledWith("Supplier Group", "Lo", 10);
		expect(w.find("datalist option").attributes("value")).toBe("Local");
	});

	it("renders hostile text as text, never HTML", () => {
		const w = mountForm({
			meta: {
				fields: [{ fieldname: "supplier_name", label: HOSTILE, fieldtype: "Data" }],
				tables: {},
			},
			values: { supplier_name: HOSTILE },
			locked: { supplier_name: HOSTILE },
		});
		expect(w.element.querySelectorAll("img, script").length).toBe(0);
		expect(w.text()).toContain(HOSTILE);
		expect(window.__pwned).toBeUndefined();
	});
});
