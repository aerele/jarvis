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

describe("email attachments", () => {
	it("shows filenames as text before approving a single email", () => {
		const w = mount(PendingCard, {
			props: {
				card: {
					kind: "email",
					to: "recipient@example.invalid",
					subject: "Report",
					attachments: ["sales.xlsx", "<img src=x onerror=alert(1)>.pdf"],
				},
				details: "",
			},
		});
		expect(w.text()).toContain("Attachments");
		expect(w.text()).toContain("sales.xlsx");
		expect(w.text()).toContain("<img src=x onerror=alert(1)>.pdf");
		expect(w.find("img").exists()).toBe(false);
	});
	it("shows each batch message's own filenames", () => {
		const w = mount(PendingCard, {
			props: {
				card: {
					kind: "bulk_email",
					count: 2,
					messages: [
						{
							recipients: "a@example.invalid",
							subject: "A",
							attachments: ["sales.xlsx"],
						},
						{
							recipients: "b@example.invalid",
							subject: "B",
							attachments: ["summary.pdf"],
						},
					],
				},
				details: "",
			},
		});
		const messages = w.findAll(".jv-rec");
		expect(messages[0].text()).toContain("sales.xlsx");
		expect(messages[0].text()).not.toContain("summary.pdf");
		expect(messages[1].text()).toContain("summary.pdf");
	});
});

// Round 2, J1-cards: the risk banner (sensitive / structural), the ONE trial warning
// line, a verb's consequence line, and a sensitive record shown in full. Every key is
// optional: a card parked before them renders exactly as before.
describe("PendingCard risk banner, warning and full values", () => {
	const RISK = { risk: "sensitive", risk_line: "This runs code for every user." };
	const WARN = { warning: { jobs: 500, rolled_back: false } };
	const kinds = {
		create: { kind: "create", doctype: "Client Script", name: null, rows: [], tables: [] },
		update: { kind: "update", doctype: "Client Script", name: "CS-1", title: "", diff: [] },
		batch_create: { kind: "batch_create", count: 1, rows: [], extra: 0, records: [] },
		bulk_update: {
			kind: "bulk_update",
			doctype: "ToDo",
			count: 1,
			records: [{ name: "T-1", title: "", fields: [], diff: [] }],
			extra: 0,
			varying: false,
		},
		verb: {
			kind: "verb",
			verb: "delete",
			action: "",
			doctype: "Client Script",
			count: 1,
			targets: ["CS-1"],
			extra: 0,
			records: [],
		},
	};

	for (const [kind, base] of Object.entries(kinds)) {
		it(`${kind}: banner first, then the warning, each a note`, () => {
			const w = mount(PendingCard, { props: { card: { ...base, ...RISK, ...WARN } } });
			const notes = w.findAll('[role="note"]');
			expect(notes.map((n) => n.text())).toEqual([
				"This runs code for every user.",
				"This will also start 500 background jobs.",
			]);
			expect(notes[0].find('[aria-hidden="true"]').exists()).toBe(true);
		});

		it(`${kind}: no banner and no warning on an ordinary card`, () => {
			const w = mount(PendingCard, { props: { card: { ...base } } });
			expect(w.findAll('[role="note"]').length).toBe(0);
		});
	}

	it("shows only the warning when the card carries no risk", () => {
		const w = mount(PendingCard, { props: { card: { ...kinds.create, ...WARN } } });
		expect(w.findAll('[role="note"]').map((n) => n.text())).toEqual([
			"This will also start 500 background jobs.",
		]);
	});

	it("a verb card says what the action does", () => {
		const card = { ...kinds.verb, consequence: "Deleting removes it permanently." };
		const w = mount(PendingCard, { props: { card } });
		expect(w.find(".jv-pcard-consequence").text()).toBe("Deleting removes it permanently.");
		const old = mount(PendingCard, { props: { card: kinds.verb } });
		expect(old.find(".jv-pcard-consequence").exists()).toBe(false);
	});

	it("a long value gets its own block, whole and escaped", () => {
		const script = "line 1\n<img src=x onerror=alert(1)>\nline 3";
		const card = {
			...kinds.create,
			...RISK,
			rows: [
				{ label: "Script", value: script, multiline: true },
				{ label: "Doctype", value: "ToDo" },
			],
		};
		const w = mount(PendingCard, { props: { card } });
		const block = w.find(".jv-pcard-longval pre");
		expect(block.element.textContent).toBe(script);
		expect(w.find(".jv-pcard-longval img").exists()).toBe(false);
		expect(w.find(".jv-pcard-fields dd:not(.jv-pcard-longval)").text()).toBe("ToDo");
	});

	it("an update with a line diff shows the changed lines and the full new text", () => {
		const card = {
			...kinds.update,
			...RISK,
			diff: [
				{
					label: "Script",
					from: "a\nb\nc",
					to: "a\nB\nc",
					lines: [
						{ op: " ", text: "a" },
						{ op: "-", text: "b" },
						{ op: "+", text: "B" },
						{ op: "gap", text: "" },
					],
				},
			],
		};
		const w = mount(PendingCard, { props: { card } });
		const lines = w.findAll(".jv-pcard-ldiff > span");
		expect(lines.map((l) => l.classes())).toEqual([
			["jv-ld-same"],
			["jv-ld-del"],
			["jv-ld-add"],
			["jv-ld-gap"],
		]);
		expect(lines[1].text()).toBe("- b");
		expect(lines[2].text()).toBe("+ B");
		const full = w.find(".jv-pcard-fulltext");
		expect(full.find("summary").text()).toBe("Full new text");
		expect(full.find("pre").element.textContent).toBe("a\nB\nc");
		expect(w.find(".jv-pcard-from").exists()).toBe(false);
	});

	it("a sensitive child-table change shows the current and the new table in full", () => {
		const table = (rows) => ({
			label: "Headers",
			count: rows.length,
			columns: ["Key", "Value"],
			fieldnames: ["key", "value"],
			rows: rows.map((cells) => ({ cells })),
			extra: 0,
			extra_columns: 0,
			unknown_columns: 0,
		});
		const card = {
			...kinds.update,
			...RISK,
			diff: [
				{
					label: "Headers",
					from: "1 row",
					to: "2 rows",
					from_table: table([["X-One", "1"]]),
					to_table: table([
						["X-One", "1"],
						["Authorization", "Bearer abc"],
					]),
				},
			],
		};
		const w = mount(PendingCard, { props: { card } });
		const tables = w.findAll(".jv-pcard-difftable");
		expect(tables.map((t) => t.find(".jv-pcard-table-head").text())).toEqual([
			"Current · 1 row",
			"New · 2 rows",
		]);
		// An emptied table names the empty side rather than dropping it.
		const emptied = mount(PendingCard, {
			props: {
				card: {
					...kinds.update,
					...RISK,
					diff: [
						{
							label: "Headers",
							from: "1 row",
							to: "0 rows",
							from_table: table([["X-One", "1"]]),
						},
					],
				},
			},
		});
		expect(
			emptied.findAll(".jv-pcard-difftable .jv-pcard-table-head").map((h) => h.text())
		).toEqual(["Current · 1 row", "New · none"]);
		expect(tables[1].text()).toContain("Bearer abc");
	});

	it("a long update value without a line diff gets blocks and its note", () => {
		const card = {
			...kinds.update,
			...RISK,
			diff: [
				{
					label: "Script",
					from: "a\nb",
					to: "a\r\nb",
					multiline: true,
					note: "Only the line endings change.",
				},
			],
		};
		const w = mount(PendingCard, { props: { card } });
		expect(w.find(".jv-pcard-diffnote").text()).toBe("Only the line endings change.");
		expect(w.find(".jv-pcard-diffblock > pre.jv-pcard-body").element.textContent).toBe(
			"a\r\nb"
		);
		expect(w.find(".jv-pcard-from").exists()).toBe(false);
	});

	it("a bulk update line diff renders with both full texts", () => {
		const card = {
			...kinds.bulk_update,
			...RISK,
			records: [
				{
					name: "T-1",
					title: "",
					fields: ["Script"],
					diff: [
						{
							label: "Script",
							from: "a\nb",
							to: "a\nB",
							lines: [{ op: "+", text: "B" }],
						},
					],
				},
			],
		};
		const w = mount(PendingCard, { props: { card } });
		expect(w.find(".jv-pcard-ldiff .jv-ld-add").text()).toBe("+ B");
		expect(w.findAll(".jv-pcard-fulltext summary").map((s) => s.text())).toEqual([
			"Full new text",
			"Current text",
		]);
	});

	it("an update row without lines keeps the from -> to line", () => {
		const card = { ...kinds.update, diff: [{ label: "Status", from: "Open", to: "Closed" }] };
		const w = mount(PendingCard, { props: { card } });
		expect(w.find(".jv-pcard-from").text()).toBe("Open");
		expect(w.find(".jv-pcard-ldiff").exists()).toBe(false);
	});
});

describe("PendingCard sensitive records and notes", () => {
	const RISK = { risk: "sensitive", risk_line: "This runs code for every user." };
	const rec = (name) => ({ name, title: "", rows: [{ label: "Script", value: "x" }] });

	it("opens every record of a sensitive verb card, only the first otherwise", () => {
		const base = {
			kind: "verb",
			verb: "delete",
			action: "",
			doctype: "Client Script",
			count: 3,
			targets: ["a", "b", "c"],
			extra: 0,
			records: [rec("a"), rec("b"), rec("c")],
		};
		const open = (card) =>
			mount(PendingCard, { props: { card } })
				.findAll("details.jv-rec")
				.map((d) => d.attributes("open") !== undefined);
		expect(open({ ...base, ...RISK })).toEqual([true, true, true]);
		expect(open(base)).toEqual([true, false, false]);
	});

	it("opens every record of a sensitive batch create and bulk update, with no click-to-review caption", () => {
		const batch = {
			kind: "batch_create",
			count: 2,
			rows: [],
			extra: 0,
			records: [
				{
					doctype: "Role",
					name: "R1",
					rows: [{ label: "Role", value: "R1" }],
					extra: 0,
					tables: [],
				},
				{
					doctype: "Role",
					name: "R2",
					rows: [{ label: "Role", value: "R2" }],
					extra: 0,
					tables: [],
				},
			],
			...RISK,
		};
		const bulk = {
			kind: "bulk_update",
			doctype: "User",
			count: 2,
			records: [
				{
					name: "u1",
					title: "",
					fields: [],
					diff: [{ label: "Enabled", from: "No", to: "Yes" }],
				},
				{
					name: "u2",
					title: "",
					fields: [],
					diff: [{ label: "Enabled", from: "No", to: "Yes" }],
				},
			],
			extra: 0,
			varying: false,
			...RISK,
		};
		for (const card of [batch, bulk]) {
			const w = mount(PendingCard, { props: { card } });
			expect(
				w.findAll("details.jv-rec").map((d) => d.attributes("open") !== undefined)
			).toEqual([true, true]);
			expect(w.text()).not.toContain("Click a record");
		}
	});

	it("shows a row's hidden line break note", () => {
		const note =
			"Contains a line break the card shows as a marker: the code may run it as a new line.";
		const card = {
			kind: "create",
			doctype: "Client Script",
			name: null,
			tables: [],
			...RISK,
			rows: [{ label: "Script", value: "x⟦U+000D⟧y", multiline: true, note }],
		};
		expect(mount(PendingCard, { props: { card } }).find(".jv-pcard-diffnote").text()).toBe(
			note
		);
	});
});

describe("PendingCard child tables on a sensitive card", () => {
	const RISK = { risk: "sensitive", risk_line: "This sends data outside the site." };
	const NOTE =
		"Contains a line break the card shows as a marker: the code may run it as a new line.";
	const table = {
		label: "Headers",
		count: 1,
		columns: ["Key", "Value"],
		rows: [{ cells: ["X-Run", "a⟦U+000D⟧b"] }],
		extra: 0,
		extra_columns: 0,
		unknown_columns: 0,
		note: NOTE,
	};

	it("a delete card shows the record's child tables with their note", () => {
		const card = {
			kind: "verb",
			verb: "delete",
			action: "",
			doctype: "Webhook",
			count: 1,
			targets: ["W"],
			extra: 0,
			records: [
				{
					name: "W",
					title: "",
					rows: [{ label: "URL", value: "https://x" }],
					tables: [table],
				},
			],
			consequence: "Deleting removes it permanently.",
			...RISK,
		};
		const w = mount(PendingCard, { props: { card } });
		const t = w.find(".jv-rec .jv-pcard-table");
		expect(t.find(".jv-pcard-table-head").text()).toBe("Headers · 1 row");
		expect(t.find(".jv-pcard-diffnote").text()).toBe(NOTE);
		expect(t.findAll("td").map((d) => d.text())).toEqual(["X-Run", "a⟦U+000D⟧b"]);
	});

	it("a create card's table shows its note", () => {
		const card = {
			kind: "create",
			doctype: "Webhook",
			name: null,
			rows: [],
			tables: [table],
			...RISK,
		};
		const w = mount(PendingCard, { props: { card } });
		expect(w.find(".jv-pcard-table .jv-pcard-diffnote").text()).toBe(NOTE);
	});
});

// The card is mounted outside the chat too (Approval Board detail panes), where
// the chat container's inline palette variables do not exist. Every palette token
// the card's styles read must therefore be the card's own --pc-* token, defined
// on the card root with the chat's light value as fallback and redefined for
// html[data-theme="dark"] with the chat's dark value. jsdom computes no CSS from
// a scoped SFC style, so this reads the stylesheet source; the rendered colours
// themselves are only checkable in a browser (the flow review).
import fs from "node:fs";
import path from "node:path";
import { LIGHT_VARS, DARK_VARS } from "@/theme";

describe("PendingCard styles stand alone outside the chat", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "PendingCard.vue"), "utf8");
	const style = src.slice(src.indexOf("<style scoped>"));
	const block = (selector) => {
		const at = style.indexOf("\n" + selector + " {"); // a rule of its own, not a descendant
		expect(at, selector).toBeGreaterThan(-1);
		return style.slice(at, style.indexOf("}", at));
	};

	it("reads no chat palette token directly", () => {
		// The --pc-* definitions themselves read the chat token first, by design.
		const rules = style.replace(/^\t--pc-[a-z0-9-]+: .*$/gm, "");
		const direct = [...rules.matchAll(/var\((--[a-z0-9-]+)/g)]
			.map((m) => m[1])
			.filter((t) => t in LIGHT_VARS);
		expect(direct).toEqual([]);
	});

	it("defines every --pc- token it reads, with the chat's light and dark values", () => {
		const used = new Set([...style.matchAll(/var\(--pc-([a-z0-9-]+)/g)].map((m) => m[1]));
		expect(used.size).toBeGreaterThan(5);
		const light = block(".jv-pcard");
		const dark = block('html[data-theme="dark"] .jv-pcard');
		for (const name of used) {
			expect(light, name).toContain(
				`--pc-${name}: var(--${name}, ${LIGHT_VARS["--" + name]});`
			);
			expect(dark, name).toContain(
				`--pc-${name}: var(--${name}, ${DARK_VARS["--" + name]});`
			);
		}
	});

	it("the banner keeps its styling hooks when mounted alone", () => {
		const card = {
			kind: "create",
			doctype: "Client Script",
			name: null,
			rows: [{ label: "Script", value: "a\nb", multiline: true }],
			tables: [],
			risk: "sensitive",
			risk_line: "This runs code for every user.",
		};
		const w = mount(PendingCard, { props: { card }, attachTo: document.body });
		expect(w.classes()).toContain("jv-pcard");
		const banner = w.find('[role="note"]');
		expect(banner.classes()).toEqual(["jv-pcard-banner", "jv-pcard-banner-sensitive"]);
		expect(banner.find("svg.jv-pcard-banner-icon").exists()).toBe(true);
		expect(w.find(".jv-pcard-longval pre.jv-pcard-body").exists()).toBe(true);
		for (const sel of [".jv-pcard-banner", ".jv-pcard-banner-icon", ".jv-pcard-body"]) {
			expect(block(sel)).toMatch(/var\(--pc-/);
		}
		w.unmount();
	});
});
