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
});
