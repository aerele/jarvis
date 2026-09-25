import { describe, expect, it } from "vitest";

import { summarize } from "./actionSummary";

// #603: a required field the model left blank used to vanish from the card, so the
// person only learned about it when Confirm failed.
describe("summarize (create)", () => {
	const field = (fieldname, label, over) => ({
		fieldname,
		label,
		value: "",
		reqd: 0,
		read_only: 0,
		proposed: true,
		...over,
	});

	it("shows a proposed required blank, flagged", () => {
		const model = {
			verb: "create",
			fields: [field("customer_name", "Customer Name", { value: "Acme", reqd: 1 })],
			tables: [],
		};
		model.fields.push(field("account_manager", "Account Manager", { reqd: 1 }));
		const action = { fields: [{ label: "Customer Name", value: "Acme" }] };
		expect(summarize(model, action).rows).toEqual([
			{ label: "Customer Name", value: "Acme" },
			{ label: "Account Manager", value: "", missing: true },
		]);
	});

	it("does not flag a required field the model never proposed", () => {
		// Controllers fill many of these on insert (a Sales Order's currency).
		const model = {
			verb: "create",
			fields: [field("currency", "Currency", { reqd: 1, proposed: false })],
			tables: [],
		};
		expect(summarize(model, {}).rows).toEqual([]);
	});

	it("does not flag an optional or read-only blank", () => {
		const model = {
			verb: "create",
			fields: [
				field("notes", "Notes"),
				field("status", "Status", { reqd: 1, read_only: 1 }),
			],
			tables: [],
		};
		expect(summarize(model, {}).rows).toEqual([]);
	});
});
