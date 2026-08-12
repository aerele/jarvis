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
