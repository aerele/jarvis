import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";

// The phone's PendingCard (pwa/src/components) is its own template; the PWA has no
// component runner (its tests are node:test over src/lib), so it is mounted here.
// vitest.config.js maps @shared to frontend/src, as pwa/vite.config.js does.
import PwaPendingCard from "../../../pwa/src/components/PendingCard.vue";

const RISK = { risk: "sensitive", risk_line: "This runs code for every user." };
const table = (rows) => ({
	label: "Headers",
	count: rows.length,
	columns: ["Key"],
	rows: rows.map((c) => ({ cells: c })),
});

describe("phone PendingCard: the round-2 card parts", () => {
	it("banner first, then the warning, each a note, and a long value in its own block", () => {
		const card = {
			kind: "create",
			doctype: "Client Script",
			name: null,
			tables: [],
			...RISK,
			warning: { jobs: 2, rolled_back: false },
			rows: [{ label: "Script", value: "a\nb", multiline: true, note: "Note." }],
		};
		const w = mount(PwaPendingCard, { props: { card } });
		expect(w.findAll('[role="note"]').map((n) => n.text())).toEqual([
			"This runs code for every user.",
			"This will also start 2 background jobs.",
		]);
		expect(w.find(".jv-pc-long pre").element.textContent).toBe("a\nb");
		expect(w.find(".jv-pc-diffnote").text()).toBe("Note.");
	});

	it("a verb's consequence and every record open on a sensitive card", () => {
		const records = ["a", "b"].map((name) => ({
			name,
			title: "",
			rows: [{ label: "Script", value: "x" }],
		}));
		const card = {
			kind: "verb",
			verb: "delete",
			action: "",
			doctype: "Client Script",
			count: 2,
			targets: ["a", "b"],
			extra: 0,
			records,
			consequence: "Deleting removes them permanently.",
		};
		const plain = mount(PwaPendingCard, { props: { card } });
		expect(plain.find(".jv-pc-consequence").text()).toBe("Deleting removes them permanently.");
		expect(
			plain.findAll("details.jv-pc-rec").map((d) => d.attributes("open") !== undefined)
		).toEqual([true, false]);
		const risky = mount(PwaPendingCard, { props: { card: { ...card, ...RISK } } });
		expect(
			risky.findAll("details.jv-pc-rec").map((d) => d.attributes("open") !== undefined)
		).toEqual([true, true]);
	});

	it("a line diff with a gap count, and both sides of a table change", () => {
		const card = {
			kind: "update",
			doctype: "Webhook",
			name: "W",
			title: "",
			...RISK,
			diff: [
				{
					label: "Script",
					from: "a\nb",
					to: "a\nB",
					lines: [
						{ op: "gap", text: "", count: 3 },
						{ op: "+", text: "B" },
					],
				},
				{
					label: "Headers",
					from: "0 rows",
					to: "1 row",
					to_table: table([["Authorization"]]),
				},
			],
		};
		const w = mount(PwaPendingCard, { props: { card } });
		expect(w.findAll(".jv-pc-ldiff > span").map((s) => s.text())).toEqual([
			"… 3 unchanged lines",
			"+ B",
		]);
		expect(w.findAll(".jv-pc-difftable .jv-pc-table-head").map((h) => h.text())).toEqual([
			"Current · none",
			"New · 1 row",
		]);
		expect(w.find(".jv-pc-difftable table").text()).toContain("Authorization");
	});

	it("a delete card shows the record's child tables with their note", () => {
		const note =
			"Contains a line break the card shows as a marker: the code may run it as a new line.";
		const t = {
			label: "Headers",
			count: 1,
			columns: ["Key"],
			rows: [{ cells: ["a⟦U+000D⟧b"] }],
			note,
		};
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
					tables: [t],
				},
			],
			...RISK,
		};
		const w = mount(PwaPendingCard, { props: { card } });
		expect(w.find(".jv-pc-rec .jv-pc-table .jv-pc-diffnote").text()).toBe(note);
		expect(w.find(".jv-pc-rec .jv-pc-table td").text()).toBe("a⟦U+000D⟧b");
	});
});
