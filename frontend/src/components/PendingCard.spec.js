import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";

import PendingCard from "./PendingCard.vue";

// The `import` confirmation-card kind (run_import). The card is the human's independent
// check on the agent, so these pin what it shows AND that untrusted file cell values can
// never be interpreted as HTML.
const importCard = (overrides = {}) => ({
	kind: "import",
	doctype: "Timesheet",
	import_type: "Insert New Records",
	file: "timesheets.csv",
	total_rows: 280,
	total_records: 280,
	submit_after_import: false,
	columns: { mapped: ["Title -> title"], unmapped: ["Journal"] },
	sample: { columns: ["Title", "Hours"], rows: [{ cells: ["REC-1", "8"] }], extra_cols: 0 },
	advisory: ["This creates 280 separate records, one per row."],
	...overrides,
});

describe("PendingCard import kind", () => {
	it("renders target, counts, file and sample rows", () => {
		const w = mount(PendingCard, { props: { card: importCard(), details: "" } });
		const text = w.text();
		expect(text).toContain("Timesheet");
		expect(text).toContain("280");
		expect(text).toContain("timesheets.csv");
		expect(w.find("table").exists()).toBe(true);
		expect(text).toContain("REC-1");
	});

	it("shows the fan-out advisory and the unmapped-columns line", () => {
		const w = mount(PendingCard, { props: { card: importCard(), details: "" } });
		expect(w.text()).toContain("separate records");
		expect(w.text()).toContain("Journal");
	});

	it("does NOT interpret an untrusted cell value as HTML (XSS-safe)", () => {
		const w = mount(PendingCard, {
			props: {
				card: importCard({
					sample: {
						columns: ["Note"],
						rows: [{ cells: ["<img src=x onerror=alert(1)>"] }],
						extra_cols: 0,
					},
				}),
				details: "",
			},
		});
		// A v-html render would create an <img>; escaped interpolation keeps it as text.
		expect(w.find("td img").exists()).toBe(false);
		expect(w.text()).toContain("<img src=x onerror=alert(1)>");
	});

	it("renders the empty state when there are no sample rows", () => {
		const w = mount(PendingCard, {
			props: {
				card: importCard({ sample: { columns: [], rows: [], extra_cols: 0 } }),
				details: "",
			},
		});
		expect(w.text()).toContain("No preview rows.");
	});

	it("shows the submit note only when submit_after_import is set", () => {
		const on = mount(PendingCard, {
			props: { card: importCard({ submit_after_import: true }), details: "" },
		});
		expect(on.text()).toMatch(/submit each record/i);
		const off = mount(PendingCard, { props: { card: importCard(), details: "" } });
		expect(off.text()).not.toMatch(/submit each record/i);
	});
});

// PlanOutline (P1, skill approve-and-run §3.5): an ADDITIVE `card.plan` field,
// rendered OUTSIDE the per-kind switch - so it must show up beside ANY kind,
// not just the "skill" kind, and must not require a CARD_KINDS entry of its
// own (the kind here is the ordinary "create" card step 1 already renders).
describe("PendingCard plan outline (card.plan, additive)", () => {
	const plannedSteps = [
		{ n: 1, verb: "create", doctype: "Sales Invoice", summary: "Acme, ₹42,000" },
		{ n: 2, verb: "submit", doctype: "Sales Invoice", summary: "that invoice" },
		{ n: 3, verb: "send_email", doctype: "", summary: "email it to a@b.com" },
		{ n: 4, verb: "delete", doctype: "Sales Invoice", summary: "the old draft DRAFT-0007" },
	];
	const createCard = (overrides = {}) => ({
		kind: "create",
		doctype: "Sales Invoice",
		rows: [{ label: "Customer", value: "Acme" }],
		tables: [],
		...overrides,
	});

	it("does NOT render when the card carries no plan", () => {
		const w = mount(PendingCard, { props: { card: createCard(), details: "" } });
		expect(w.text()).not.toContain("This run will do");
	});

	it("renders beside a plain 'create' kind card when card.plan.steps is present", () => {
		const w = mount(PendingCard, {
			props: {
				card: createCard({ plan: { steps: plannedSteps } }),
				details: "",
			},
		});
		const text = w.text();
		// The card's own kind rendering is untouched.
		expect(text).toContain("Create Sales Invoice");
		// The outline sits alongside it.
		expect(text).toContain("This run will do 4 steps");
		expect(text).toContain("Acme, ₹42,000");
		expect(text).toContain("email it to a@b.com");
	});

	it("marks a delete/cancel/amend step destructive - 'will still ask when reached' - and leaves the rest alone", () => {
		const w = mount(PendingCard, {
			props: { card: createCard({ plan: { steps: plannedSteps } }), details: "" },
		});
		const rows = w.findAll(".jv-plan-row");
		expect(rows.length).toBe(4);
		expect(rows[3].classes()).toContain("jv-plan-row--destructive");
		expect(rows[3].text()).toContain("will still ask when reached");
		expect(rows[0].classes()).not.toContain("jv-plan-row--destructive");
		expect(rows[0].text()).not.toContain("will still ask when reached");
	});

	it("marks step 1 verified and later steps planned when the data does not say otherwise", () => {
		const w = mount(PendingCard, {
			props: { card: createCard({ plan: { steps: plannedSteps } }), details: "" },
		});
		const rows = w.findAll(".jv-plan-row");
		expect(rows[0].classes()).toContain("jv-plan-row--verified");
		expect(rows[0].text()).toContain("verified");
		expect(rows[1].classes()).not.toContain("jv-plan-row--verified");
		expect(rows[1].text()).toContain("planned");
	});

	it("respects an explicit server-sent `verified` flag over the n===1 fallback", () => {
		const w = mount(PendingCard, {
			props: {
				card: createCard({
					plan: {
						steps: [
							{
								n: 1,
								verb: "create",
								doctype: "Sales Invoice",
								summary: "x",
								verified: false,
							},
							{
								n: 2,
								verb: "submit",
								doctype: "Sales Invoice",
								summary: "y",
								verified: true,
							},
						],
					},
				}),
				details: "",
			},
		});
		const rows = w.findAll(".jv-plan-row");
		expect(rows[0].classes()).not.toContain("jv-plan-row--verified");
		expect(rows[1].classes()).toContain("jv-plan-row--verified");
	});

	it("caps the visible rows and shows a '+N more' overflow", () => {
		const steps = Array.from({ length: 25 }, (_, i) => ({
			n: i + 1,
			verb: "create",
			doctype: "Task",
			summary: `Task ${i + 1}`,
		}));
		const w = mount(PendingCard, {
			props: { card: createCard({ plan: { steps } }), details: "" },
		});
		expect(w.findAll(".jv-plan-row").length).toBe(20);
		expect(w.text()).toContain("+5 more");
	});
});
