import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The read-only view of another user's skill. What matters: it shows the shares, the
 * allowed roles, the lineage, the history and how many macros use the skill; nothing
 * in it edits the skill; the instructions are another user's free text, printed as
 * plain text in a <pre> and never through the Markdown renderer.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "iconLeft", "loading"],
		emits: ["click"],
		template: `<button class="stub-button" @click="$emit('click')">{{ label }}</button>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i class="stub-icon" />` },
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><h4>{{ options && options.title }}</h4><slot name="body-content" /><slot name="actions" /></div>`,
	},
}));

vi.mock("@/utils/datetime", () => ({
	timeAgo: (d) => (d ? "a while ago" : ""),
	exactDate: (d) => String(d || ""),
}));

// A Markdown renderer anywhere in this dialog would be a leak: it must never be asked.
const markdown = vi.hoisted(() => ({ renderMarkdown: vi.fn(() => "<p>rendered</p>") }));
vi.mock("@/markdown", () => markdown);

const api = vi.hoisted(() => ({ adminGetSkill: vi.fn() }));
vi.mock("@/api/skillsAdmin", () => api);

import SkillAdminDialog from "./SkillAdminDialog.vue";

const HOSTILE = `<img src=x onerror=alert(1)> **bold** # not a heading`;
const skill = (extra = {}) => ({
	name: "s1",
	skill_name: "month-end",
	description: "Closes the month",
	owner: "asha@example.test",
	owner_name: "Asha Rao",
	scope: "User",
	target_role: "",
	enabled: 1,
	allow_approve_run: 0,
	admin_hold: 0,
	admin_hold_reason: "",
	shared_with_count: 1,
	modified: "2026-10-01 10:00:00",
	is_mine: 0,
	is_published: 0,
	is_learned: 0,
	instructions: "Step one\nStep two",
	shared_with: [{ user: "ben@example.test", full_name: "Ben Rao" }],
	allowed_roles: [],
	lineage: null,
	history: [],
	used_by_macros: { total: 0, armed: 0 },
	...extra,
});

async function open(extra, props = {}) {
	api.adminGetSkill.mockResolvedValue(skill(extra));
	const w = mount(SkillAdminDialog, { props: { modelValue: true, name: "s1", ...props } });
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("SkillAdminDialog", () => {
	it("asks for the skill by name when it opens, and titles itself with it", async () => {
		const w = await open();
		expect(api.adminGetSkill).toHaveBeenCalledWith("s1");
		expect(w.find("h4").text()).toBe("month-end");
		expect(w.find(".jv-skill-admin-note").text()).toContain(
			"Asha Rao (asha@example.test)'s own instructions, shown read-only as plain text"
		);
	});

	it("prints the instructions as plain text in a pre block, never through Markdown", async () => {
		const w = await open({ instructions: HOSTILE });
		const pre = w.find("pre.jv-skill-admin-text");
		expect(pre.exists()).toBe(true);
		expect(pre.text()).toBe(HOSTILE);
		// The hostile text is text, not an element.
		expect(w.find("img").exists()).toBe(false);
		expect(pre.element.querySelector("*")).toBeNull();
		expect(markdown.renderMarkdown).not.toHaveBeenCalled();
		expect(w.html()).not.toContain("v-html");
	});

	it("shows the state in words: on or off, audience, writes without asking, on hold and why", async () => {
		const w = await open({
			enabled: 0,
			scope: "Role",
			target_role: "Accounts User",
			allow_approve_run: 1,
			admin_hold: 1,
			admin_hold_reason: "Emails customers",
		});
		const dd = w.findAll("dd")[1];
		expect(dd.findAll(".badge").map((b) => b.text())).toEqual([
			"Off",
			"Role: Accounts User",
			"Writes without asking",
			"On hold",
		]);
		expect(w.find(".jv-skill-admin-dialog-reason").text()).toBe("Emails customers");
		expect(w.text()).not.toMatch(/\bOrg\b|scope|armed/i);
	});

	it("says a hold has no reason given when it has none", async () => {
		const w = await open({ admin_hold: 1, admin_hold_reason: "" });
		expect(w.find(".jv-skill-admin-dialog-reason").text()).toBe("None given");
	});

	it("lists who it is shared with, or says nobody", async () => {
		let w = await open();
		expect(w.findAll(".jv-skill-admin-shares li").map((l) => l.text())).toEqual([
			"Ben Rao (ben@example.test)",
		]);
		w = await open({ shared_with: [] });
		expect(w.text()).toContain("Nobody.");
	});

	it("lists the roles that can use it, only when there are some", async () => {
		let w = await open();
		expect(w.find(".jv-skill-admin-roles").exists()).toBe(false);
		w = await open({ scope: "Role", allowed_roles: ["Accounts User", "Accounts Manager"] });
		expect(w.find(".jv-skill-admin-roles").text()).toBe("Accounts User, Accounts Manager");
	});

	it("shows where a published skill came from", async () => {
		let w = await open();
		expect(w.find(".jv-skill-admin-lineage").exists()).toBe(false);
		w = await open({
			lineage: {
				source_skill: "month-end-draft",
				author: "asha@example.test",
				author_name: "Asha Rao",
			},
		});
		expect(w.find(".jv-skill-admin-lineage").text()).toBe(
			"month-end-draft, written by Asha Rao"
		);
	});

	it("says how many macros use it, in plain words", async () => {
		let w = await open();
		expect(w.find(".jv-skill-admin-macros").text()).toBe("No macro uses it.");
		w = await open({ used_by_macros: { total: 3, armed: 1 } });
		expect(w.find(".jv-skill-admin-macros").text()).toBe(
			"3 macros use it (1 of them writes without asking)."
		);
		expect(w.text()).not.toMatch(/armed/i);
	});

	it("shows the admin history as text, or says none has acted", async () => {
		let w = await open();
		expect(w.find(".jv-skill-admin-history").exists()).toBe(false);
		expect(w.text()).toContain("No administrator has acted on this skill.");
		w = await open({
			history: [
				{
					admin: "boss@example.test",
					admin_name: "Boss Person",
					verb: "hold",
					reason: HOSTILE,
					at: "2026-10-02 09:00:00",
				},
				{
					admin: "boss@example.test",
					admin_name: "",
					verb: "release",
					reason: "",
					at: "2026-10-03 09:00:00",
				},
			],
		});
		const cells = w
			.findAll(".jv-skill-admin-history tbody tr")
			.map((tr) => tr.findAll("td").map((td) => td.text()));
		expect(cells).toEqual([
			["a while ago", "Boss Person", "Put on hold", HOSTILE],
			["a while ago", "boss@example.test", "Released", "-"],
		]);
		expect(w.find(".jv-skill-admin-history img").exists()).toBe(false);
	});

	it("keeps the body in one scrolling region the keyboard can reach, with nothing to edit", async () => {
		const w = await open();
		const body = w.find(".jv-skill-admin-body");
		expect(body.classes()).toContain("overflow-y-auto");
		expect(body.attributes("tabindex")).toBe("0");
		expect(body.attributes("role")).toBe("region");
		expect(w.find("textarea").exists()).toBe(false);
		expect(w.find("input").exists()).toBe(false);
		expect(w.findAll("button.stub-button").map((b) => b.text())).toEqual(["Close"]);
	});

	it("says it was deleted, and tells the tab, when the skill is gone", async () => {
		api.adminGetSkill.mockRejectedValue({ status: 404, exc_type: "DoesNotExistError" });
		const w = mount(SkillAdminDialog, { props: { modelValue: true, name: "s1" } });
		await flushPromises();
		expect(w.find(".jv-skill-admin-gone").text()).toBe("This skill was deleted.");
		expect(w.emitted("gone")).toEqual([["s1"]]);
	});

	it("shows another failure with Try again, and recovers", async () => {
		api.adminGetSkill.mockRejectedValueOnce(new Error("Server is busy"));
		const w = mount(SkillAdminDialog, { props: { modelValue: true, name: "s1" } });
		await flushPromises();
		expect(w.find("[role='alert']").text()).toContain("Server is busy");
		api.adminGetSkill.mockResolvedValue(skill());
		await w
			.findAll("button.stub-button")
			.find((b) => b.text() === "Try again")
			.trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(w.find("pre").exists()).toBe(true);
	});

	it("only fills with the newest request when pointed at another skill", async () => {
		let slow;
		api.adminGetSkill.mockReturnValueOnce(new Promise((r) => (slow = r)));
		const w = mount(SkillAdminDialog, { props: { modelValue: true, name: "s1" } });
		api.adminGetSkill.mockResolvedValue(skill({ name: "s2", skill_name: "second" }));
		await w.setProps({ name: "s2" });
		await flushPromises();
		slow(skill({ skill_name: "first" }));
		await flushPromises();
		expect(w.find("h4").text()).toBe("second");
	});
});
