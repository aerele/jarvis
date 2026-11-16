import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import fs from "fs";
import path from "path";
import DraftCard from "./DraftCard.vue";

const FIXTURES = JSON.parse(
	fs.readFileSync(path.resolve(__dirname, "../../lib/draftCardFixtures.json"), "utf8")
);
const card = (id) => FIXTURES.find((f) => f.id === id).card;
const draw = (id, ctx = {}) => mount(DraftCard, { props: { card: card(id), ctx } });
const labelled = (w, label) => w.findAll("button").find((b) => b.text() === label);

describe("DraftCard", () => {
	// Break: a button emits the wrong event, or twice.
	it.each([
		["Create", "create"],
		["Preview", "preview"],
		["Edit", "edit"],
		["Cancel", "cancel"],
	])("%s emits %s once", async (label, event) => {
		const w = draw("create-with-table");
		await labelled(w, label).trigger("click");
		expect(Object.keys(w.emitted())).toContain(event);
		expect(w.emitted(event)).toHaveLength(1);
	});

	// Break: the update primary emits something the host does not map to a confirm.
	it("Save changes emits create", async () => {
		const w = draw("update-diff");
		await labelled(w, "Save changes").trigger("click");
		expect(w.emitted("create")).toHaveLength(1);
	});

	// Break: a locked or spinning button still reaches the host (double confirm).
	it.each([[{ busy: "create" }], [{ streaming: true }], [{ editing: true }]])(
		"a disabled button emits nothing %j",
		async (ctx) => {
			const w = draw("create-simple", ctx);
			for (const b of w
				.findAll("button")
				.filter((x) => x.attributes("disabled") !== undefined))
				await b.trigger("click");
			expect(Object.keys(w.emitted())).toEqual([]);
		}
	);

	it("Cancel stays live while editing and the pressed button is aria-busy", async () => {
		const editing = draw("create-simple", { editing: true });
		await labelled(editing, "Cancel").trigger("click");
		expect(editing.emitted("cancel")).toHaveLength(1);
		const busy = draw("create-simple", { busy: "create" });
		expect(labelled(busy, "Create").attributes("aria-busy")).toBe("true");
	});

	// Break: an unreadable card draws a blank card or loses its way out.
	it("S12 says so and Cancel works", async () => {
		const w = draw("unreadable");
		expect(w.text()).toContain("Couldn't show this draft. Ask Jarvis to send it again.");
		expect(w.findAll("button").map((b) => b.text())).toEqual(["Cancel"]);
		await w.find("button").trigger("click");
		expect(w.emitted("cancel")).toHaveLength(1);
	});

	// Break: the card draws the title, the table without the dropped column, the ignored note.
	it("draws title, fields, table and note", () => {
		const t = draw("create-with-table");
		expect(t.find("h3").text()).toBe("New Web Form");
		expect(t.text()).toContain("Feedback");
		expect(t.findAll("th").map((h) => h.text())).toEqual([
			"Field",
			"Fieldtype",
			"Custom Label",
			"Mandatory",
		]);
		expect(draw("create-ignored-field").text()).toContain(
			"Left out: colour. These are not fields on this form."
		);
	});

	// Break: a raw tool name reaches a user.
	it.each(FIXTURES.map((f) => f.id))("%s shows no tool name", (id) => {
		const w = draw(id);
		expect(w.text()).not.toMatch(/create_doc|update_doc/);
	});

	// Break: a sensitive card gets a button here, so a host that mounts it wrongly confirms blind.
	it("S11 renders no button and says it needs confirmation", () => {
		const w = draw("sensitive");
		expect(w.findAll("button")).toHaveLength(0);
		expect(w.text()).toBe("This change needs your confirmation.");
	});

	// Break: model-written text is parsed as HTML.
	it("hostile text is drawn as text", () => {
		const evil = "<img src=x onerror=alert(1)>";
		const c = structuredClone(card("create-with-table"));
		c.draft.title = evil;
		c.tables[0].rows[0].cells[0] = evil;
		const w = mount(DraftCard, { props: { card: c } });
		expect(w.find("img").exists()).toBe(false);
		expect(w.find("h3").text()).toBe(evil);
		expect(w.find("td").text()).toBe(evil);
	});

	// Break: the Earlier tag, the footer lines or the "+N more" note are computed but not drawn.
	it("draws the tag, the footers and the row note", () => {
		expect(draw("create-simple", { earlier: true }).find(".dc-tag").text()).toBe("Earlier");
		expect(draw("create-simple", { editing: true }).text()).toContain("Editing");
		expect(draw("create-simple", { streaming: true }).text()).toContain("Jarvis is replying");
		// the place in a stack is drawn next to the Earlier tag, and absent for a card alone
		const stacked = draw("create-simple", { earlier: true, position: 2, count: 3 });
		expect(stacked.findAll(".dc-tag").map((t) => t.text())).toEqual(["Earlier", "2 of 3"]);
		expect(draw("create-simple", { position: 1, count: 1 }).find(".dc-num").exists()).toBe(
			false
		);
		const c = structuredClone(card("create-with-table"));
		c.tables[0].count = 12;
		c.tables[0].rows = Array.from({ length: 12 }, (_, i) => ({
			cells: [`r${i}`, "", "", "", ""],
		}));
		expect(mount(DraftCard, { props: { card: c } }).text()).toContain("+4 more");
	});

	// Break: the trial warning is computed but not drawn, or drawn when absent.
	it("draws the trial warning as a note only when present", () => {
		const c = { ...card("create-simple"), warning: { jobs: 1 } };
		const w = mount(DraftCard, { props: { card: c } });
		expect(w.find("[role=note]").text()).toBe("This will also start 1 background job.");
		expect(draw("create-simple").find("[role=note]").exists()).toBe(false);
	});

	// Break: an unreadable card stays pressable while its Cancel is in flight.
	it("S12 under busy cancel is disabled and spinning", async () => {
		const w = draw("unreadable", { busy: "cancel" });
		const b = w.find("button");
		expect(b.attributes("disabled")).toBeDefined();
		expect(b.attributes("aria-busy")).toBe("true");
		await b.trigger("click");
		expect(w.emitted("cancel")).toBeUndefined();
	});

	// Break: a removal is computed but not drawn, or an old value is not shown beside the new.
	it("an update lists the rows it removes and what a changed cell was", () => {
		const w = draw("update-table-rows");
		expect(w.find("h4").text()).toBe("Will be removed");
		const removed = w.find(".dc-removed");
		expect(removed.findAll("td").map((c) => c.text())).toEqual(["13:00:00", "0.5"]);
		const changed = w.findAll("tr").find((r) => r.text().includes("11:00:00"));
		expect(changed.find("s").text()).toContain("10:30:00");
		expect(changed.find(".dc-now").text()).toContain("11:00:00");
		expect(changed.find(".dc-status").text()).toBe("Changed");
		expect(w.text()).toContain("1 row changed, 1 row added, 1 row will be removed");
		expect(w.text()).not.toContain("3 rows");
	});

	// Break: model-written cells inside row_changes are parsed as HTML.
	it("hostile row_changes text is drawn as text", () => {
		const evil = "<img src=x onerror=alert(1)>";
		const c = structuredClone(card("update-table-rows"));
		const rc = c.diff[0].row_changes;
		rc.rows[1].cells[0] = evil;
		rc.rows[1].was[0] = evil + "1";
		rc.removed[0].cells[0] = evil;
		const w = mount(DraftCard, { props: { card: c } });
		expect(w.find("img").exists()).toBe(false);
		expect(w.find(".dc-removed td").text()).toBe(evil);
	});

	// Break: the status footer is inserted with its text, so a screen reader never announces it.
	it("the live region is always there and only its text changes", () => {
		const idle = draw("create-simple");
		expect(idle.find("[aria-live=polite]").exists()).toBe(true);
		expect(idle.find("[aria-live=polite]").text()).toBe("");
		const busy = draw("create-simple", { streaming: true });
		expect(busy.find("[aria-live=polite]").text()).toBe("Jarvis is replying");
	});

	// Break: a hidden change or a reorder is computed but not drawn.
	it("draws the hidden-change note and the reorder line", () => {
		const c = structuredClone(card("update-diff"));
		c.diff[0] = {
			label: "Items",
			from: "2 rows",
			to: "2 rows",
			row_changes: {
				columns: ["Qty"],
				rows: [
					{ status: "changed", hidden_change: true, cells: ["5"], was: ["5"] },
					{ status: "kept", cells: ["1"] },
				],
				reordered: true,
			},
		};
		const w = mount(DraftCard, { props: { card: c } });
		expect(w.find(".dc-note-row").text()).toBe("A value not shown here changes.");
		expect(w.find(".dc-note-row td").attributes("colspan")).toBe("2");
		expect(w.text()).toContain("The rows are saved in the order shown.");
		expect(w.text()).toContain("1 row changed, 1 row unchanged, row order changes");
	});

	// Break: the removed list loses its header semantics, or Was/Now lose their labels or breaks.
	it("the removed table has a real header and a multi-line entry stacks Was over Now", () => {
		const w = draw("update-table-rows");
		const head = w.find(".dc-removed thead");
		expect(head.classes()).not.toContain("dc-sr");
		expect(head.findAll("th").map((h) => h.text())).toEqual(["From Time", "Hrs"]);
		const c = structuredClone(card("update-diff"));
		c.diff[0] = { label: "Notes", from: "a\nb", to: "c\nd", multiline: true };
		const m = mount(DraftCard, { props: { card: c } });
		const blocks = m.findAll(".dc-block");
		expect(blocks.map((b) => b.find(".dc-block-label").text())).toEqual(["Was", "Now"]);
		expect(blocks[0].text()).toContain("a\nb");
		expect(blocks[1].text()).toContain("c\nd");
	});

	// Break: the footer live region is missing in a state where it is needed.
	it.each([[{}], [{ editing: true }], [{ streaming: true }]])(
		"the live region exists for %j",
		(ctx) => {
			expect(draw("create-simple", ctx).find("[aria-live=polite]").exists()).toBe(true);
		}
	);

	// Break: the could-not-compare caveat is computed but not drawn as a note.
	it("draws the unverified note as a note", () => {
		const c = structuredClone(card("update-diff"));
		c.diff[0] = {
			label: "Items",
			row_changes: { columns: ["A"], rows: [{ cells: ["a"] }], unverified: true },
		};
		const w = mount(DraftCard, { props: { card: c } });
		const note = w.findAll("[role=note]").find((n) => n.text().includes("fully compared"));
		expect(note.text()).toBe(
			"This table could not be fully compared. Check its rows before you save."
		);
		expect(w.find(".dc-unchanged").text()).toBe("Not compared");
	});

	// Break: an entry the server could not compare at all draws no caveat under its text.
	it("draws the note for an entry flagged unverified, with its plain rows text", () => {
		const c = structuredClone(card("update-diff"));
		c.diff[0] = { label: "Items", from: "3 rows", to: "3 rows", unverified: true };
		const w = mount(DraftCard, { props: { card: c } });
		expect(w.find("[role=note]").text()).toBe(
			"This table could not be fully compared. Check its rows before you save."
		);
		expect(w.find(".dc-old").text()).toBe("3 rows");
	});

	// Break: the long scroll box cannot be reached or named by keyboard and screen reader users.
	it.each([
		[13, true],
		[12, false],
	])("a box of %i shown rows is focusable: %s", (n, tall) => {
		const c = structuredClone(card("update-diff"));
		const rows = Array.from({ length: n }, (_, i) => ({ status: "new", cells: ["r" + i] }));
		c.diff[0] = { label: "Items", row_changes: { columns: ["A"], rows } };
		const box = mount(DraftCard, { props: { card: c } }).find(".dc-changes .dc-scroll");
		expect(box.classes().includes("dc-tall")).toBe(tall);
		expect(box.attributes("tabindex")).toBe(tall ? "0" : undefined);
		expect(box.attributes("aria-label")).toBe(tall ? "Rows this update changes" : undefined);
		expect(box.attributes("role")).toBe(tall ? "region" : undefined);
	});

	// Break: the phone app gets the small desktop buttons on a wide phone or a tablet.
	it.each([
		["phone", true],
		["desktop", false],
		[undefined, false],
	])("surface %s puts the touch class on the root: %s", (surface, touch) => {
		const w = draw("update-diff", surface ? { surface } : {});
		expect(w.classes().includes("dc-phone")).toBe(touch);
	});
});
