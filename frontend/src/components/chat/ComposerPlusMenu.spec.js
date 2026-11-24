// The composer's "+" menu: files, a skill, a connector focus and wiki grounding.
import { afterEach, describe, expect, test } from "vitest";
import { mount } from "@vue/test-utils";
import ComposerPlusMenu from "./ComposerPlusMenu.vue";

const skills = [
	{ name: "s1", skill_name: "month-end", description: "Close the books", enabled: 1 },
	{ name: "s2", skill_name: "invoicing", description: "Raise sales invoices", enabled: 1 },
	{ name: "s3", skill_name: "draft-thing", description: "Not ready", enabled: 0 },
	{ name: "s4", skill_name: "Bad Name", description: "no token", enabled: 1 },
	{
		name: "s5",
		skill_name: "gst-filing",
		description: "Monthly GST",
		enabled: 1,
		shared_by: "Asha",
	},
];
const connectors = [
	{ key: "gh", label: "GitHub", preset: "github" },
	{ key: "sl", label: "Slack", preset: "slack" },
];

let w;
function make(props = {}) {
	w = mount(ComposerPlusMenu, {
		props: { skills, connectors, wikiAvailable: true, ...props },
		attachTo: document.body,
	});
	return w;
}
async function openMenu() {
	await w.find(".jv-plus-btn").trigger("click");
}
const labels = () => w.findAll(".jv-plus-panel .jv-plus-label").map((l) => l.text());

describe("ComposerPlusMenu", () => {
	afterEach(() => w?.unmount());

	test("opens on +, lists the four things and tells the host to refresh", async () => {
		make();
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
		await openMenu();
		expect(w.emitted("open")).toHaveLength(1);
		expect(w.find(".jv-plus-btn").attributes("aria-expanded")).toBe("true");
		expect(labels()).toEqual([
			"Add files or photos",
			"Use a skill",
			"Connectors",
			"Ground in wiki",
		]);
	});

	test("Add files asks the host for its picker and closes", async () => {
		make();
		await openMenu();
		await w.findAll(".jv-plus-item")[0].trigger("click");
		expect(w.emitted("attach")).toHaveLength(1);
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
	});

	test("skills: only ones the server would honour, searchable, and a pick arms it", async () => {
		make();
		await openMenu();
		await w.findAll(".jv-plus-item")[1].trigger("click");
		expect(labels()).toEqual(["/month-end", "/invoicing", "/gst-filing"]);
		expect(w.text()).toContain("Shared by Asha");
		await w.find(".jv-plus-search").setValue("sales");
		expect(labels()).toEqual(["/invoicing"]);
		await w.find(".jv-plus-search").setValue("zzz");
		expect(w.find(".jv-plus-empty").text()).toBe("No skill matches that.");
		await w.find(".jv-plus-search").setValue("");
		await w.findAll(".jv-plus-item")[2].trigger("click");
		expect(w.emitted("skill")[0][0].skill_name).toBe("gst-filing");
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
	});

	test("with no skills it says how to get one", async () => {
		make({ skills: [] });
		await openMenu();
		await w.findAll(".jv-plus-item")[1].trigger("click");
		expect(w.find(".jv-plus-empty").text()).toContain("No skills yet");
	});

	test("what is on shows as pills beside +, each × clears its own; off when the host shows them", async () => {
		make({ selectedSkill: "invoicing", wikiOn: true, focus: { key: "gh", label: "GitHub" } });
		const pills = w.findAll(".jv-skillpill");
		expect(pills.map((p) => p.find(".jv-skillpill-text").text())).toEqual([
			"invoicing",
			"Wiki",
			"GitHub",
		]);
		await pills[0].find(".jv-skillpill-x").trigger("click");
		await pills[1].find(".jv-skillpill-x").trigger("click");
		await pills[2].find(".jv-skillpill-x").trigger("click");
		expect(w.emitted("skill")).toEqual([[null]]);
		expect(w.emitted("wiki")).toEqual([[false]]);
		expect(w.emitted("connector")).toEqual([[null]]);
		w.unmount();
		make({ selectedSkill: "invoicing", wikiOn: true, showPill: false });
		expect(w.find(".jv-skillpill").exists()).toBe(false);
	});

	test("connectors: pick one, see it ticked, clear it", async () => {
		make({ focus: { key: "sl", label: "Slack" } });
		await openMenu();
		expect(w.findAll(".jv-plus-hint").map((h) => h.text())).toContain("Slack");
		await w.findAll(".jv-plus-item")[2].trigger("click");
		expect(labels()).toEqual(["Clear focus", "GitHub", "Slack"]);
		const slack = w.findAll(".jv-plus-item")[2];
		expect(slack.attributes("aria-checked")).toBe("true");
		await w.findAll(".jv-plus-item")[1].trigger("click");
		expect(w.emitted("connector")[0][0].key).toBe("gh");
		await openMenu();
		await w.findAll(".jv-plus-item")[2].trigger("click");
		await w.findAll(".jv-plus-item")[0].trigger("click");
		expect(w.emitted("connector")[1]).toEqual([null]);
	});

	test("wiki is a check item: ticked when on, a tap flips it", async () => {
		make({ wikiOn: true });
		await openMenu();
		const wiki = w.findAll(".jv-plus-item")[3];
		expect(wiki.attributes("role")).toBe("menuitemcheckbox");
		expect(wiki.attributes("aria-checked")).toBe("true");
		expect(wiki.find(".jv-plus-check").exists()).toBe(true);
		await wiki.trigger("click");
		expect(w.emitted("wiki")).toEqual([[false]]);
		w.unmount();
		make({ wikiOn: false });
		await openMenu();
		expect(w.findAll(".jv-plus-item")[3].find(".jv-plus-check").exists()).toBe(false);
	});

	test("an empty wiki shows why, and does nothing; a wiki that is off is not offered", async () => {
		make({ wikiAvailable: false, wikiEmpty: true });
		await openMenu();
		const wiki = w.findAll(".jv-plus-item")[3];
		expect(wiki.text()).toContain("No wiki pages yet");
		expect(wiki.attributes("aria-disabled")).toBe("true");
		await wiki.trigger("click");
		expect(w.emitted("wiki")).toBeUndefined();
		w.unmount();
		make({ wikiAvailable: false });
		await openMenu();
		expect(labels()).not.toContain("Ground in wiki");
	});

	test("the panel lines up with the composer box and opens above it", async () => {
		const box = document.createElement("div");
		box.className = "jv-composer";
		document.body.appendChild(box);
		w = mount(ComposerPlusMenu, { props: { skills }, attachTo: box });
		box.getBoundingClientRect = () => ({ left: 100, top: 500, right: 700, bottom: 600 });
		w.find(".jv-plus").element.getBoundingClientRect = () => ({
			left: 112,
			top: 560,
			right: 142,
			bottom: 590,
		});
		await openMenu();
		const style = w.find(".jv-plus-panel").attributes("style");
		expect(style).toContain("left: -12px");
		expect(style).toContain("bottom: 98px"); // 590 - 500 + 8: just above the box
		w.unmount();
		box.remove();
		w = null;
	});

	test("Escape steps back from a sub-list, then closes, wherever focus is", async () => {
		make();
		await openMenu();
		await w.findAll(".jv-plus-item")[2].trigger("click"); // Connectors
		document.body.focus(); // the row that was clicked is gone
		const esc = () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
		esc();
		await w.vm.$nextTick();
		expect(labels()[0]).toBe("Add files or photos");
		esc();
		await w.vm.$nextTick();
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
		expect(document.activeElement).toBe(w.find(".jv-plus-btn").element);
	});

	test("opening puts focus on the first row; the skills list on its search", async () => {
		make();
		await openMenu();
		await w.vm.$nextTick();
		expect(document.activeElement.textContent).toContain("Add files or photos");
		await w.findAll(".jv-plus-item")[1].trigger("click");
		await w.vm.$nextTick();
		expect(document.activeElement).toBe(w.find(".jv-plus-search").element);
	});

	test("a click outside closes it; disabled never opens", async () => {
		make();
		await openMenu();
		document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
		await w.vm.$nextTick();
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
		w.unmount();
		make({ disabled: true });
		await openMenu();
		expect(w.find(".jv-plus-panel").exists()).toBe(false);
		expect(w.emitted("open")).toBeUndefined();
	});
});
