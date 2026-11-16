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
});
