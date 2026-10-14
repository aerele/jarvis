import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The question an admin answers before putting another user's skill on hold: why.
 * What matters: no reason, no hold; a reason over the server's limit is refused here
 * first; the hold is sent with the reason as typed (trimmed); a refusal stays in the
 * dialog; a reason typed and cancelled is still there when the same skill's dialog
 * opens again; a company skill's copy says it stops for everyone.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "loading", "disabled", "theme"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" @click="$emit('click')">{{ label }}</button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><button class="stub-close">x</button><slot name="body-title"><h4>{{ options && options.title }}</h4></slot><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span class="stub-error">{{ message }}</span>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "modelValue", "label", "disabled"],
		emits: ["update:modelValue"],
		template: `<textarea class="stub-reason" :value="modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

const api = vi.hoisted(() => ({ adminHold: vi.fn() }));
vi.mock("@/api/skillsAdmin", () => api);

import { toast } from "frappe-ui";
import SkillHoldDialog from "./SkillHoldDialog.vue";

function open(props = {}) {
	return mount(SkillHoldDialog, {
		attachTo: document.body,
		props: {
			modelValue: true,
			name: "s1",
			skillName: "month-end",
			ownerLabel: "Asha Rao (asha@example.test)",
			...props,
		},
	});
}
const holdButton = (w) =>
	w.findAll("button.stub-button").find((b) => /^(Put on hold|Hold for everyone)$/.test(b.text()));
const used = (total, armed = 0) => ({ total, armed });

beforeEach(() => {
	vi.clearAllMocks();
	api.adminHold.mockResolvedValue({ ok: true, needs_apply: false, used_by_macros: used(0) });
});

describe("SkillHoldDialog", () => {
	it("names the skill and its owner, and says what a hold does and does not undo", () => {
		const w = open();
		expect(w.find("h3").text()).toBe("Put on hold: month-end");
		expect(w.find(".jv-skill-hold-owner").text()).toBe("Owned by Asha Rao (asha@example.test)");
		expect(w.find("h3").classes()).toContain("jv-skill-dialog-title");
		expect(w.text()).toContain("The assistant stops using this skill until an admin releases it");
		expect(w.text()).toContain("Its owner sees the reason on the skill.");
		// Releasing is not "resume": said before the hold, not after.
		expect(w.find(".jv-skill-hold-note").text()).toContain(
			"Releasing the hold later does not undo these"
		);
		expect(w.find(".jv-skill-hold-note").text()).toContain("does not write without asking again");
		expect(w.find(".jv-skill-hold-company").exists()).toBe(false);
		expect(w.text()).not.toMatch(/\bOrg\b|scope|armed/i);
	});

	it("says more for a skill shared with everyone: it stops for everyone, and Apply is needed", () => {
		const w = open({ company: true });
		expect(w.find("h3").text()).toBe("Hold for everyone: month-end");
		expect(w.find(".jv-skill-hold-company").text()).toContain("so it stops for everyone");
		expect(w.find(".jv-skill-hold-company").text()).toContain("press Apply");
		expect(holdButton(w).text()).toBe("Hold for everyone");
		expect(w.text()).not.toMatch(/\bOrg\b|scope|armed/i);
	});

	it("will not hold without a reason", async () => {
		const w = open();
		expect(holdButton(w).attributes("disabled")).toBe("");
		await w.find(".stub-reason").setValue("   ");
		expect(holdButton(w).attributes("disabled")).toBe("");
		await holdButton(w).trigger("click");
		expect(api.adminHold).not.toHaveBeenCalled();
	});

	it("refuses a reason over 500 characters before asking the server", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("x".repeat(501));
		expect(holdButton(w).attributes("disabled")).toBe("");
		expect(w.text()).toContain("501 / 500");
		await w.find(".stub-reason").setValue("x".repeat(500));
		expect(holdButton(w).attributes("disabled")).toBeUndefined();
	});

	it("starts with the reason box focused, not the close button", async () => {
		const w = open();
		await flushPromises();
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).toBe(w.find(".stub-reason").element);
		w.unmount();
	});

	it("holds with the reason as typed, says so, tells the tab and closes", async () => {
		api.adminHold.mockResolvedValue({ ok: true, needs_apply: true, used_by_macros: used(2, 1) });
		const w = open();
		await w.find(".stub-reason").setValue("  Emails customers  ");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHold).toHaveBeenCalledWith("s1", "Emails customers");
		expect(toast.success).toHaveBeenCalledWith("“month-end” is on hold; 2 macros use it");
		expect(w.emitted("held")).toEqual([[{ name: "s1", needs_apply: true }]]);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("puts the skill's name into the toast escaped", async () => {
		const w = open({ skillName: "<b>x</b>" });
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(toast.success).toHaveBeenCalledWith("“&lt;b&gt;x&lt;/b&gt;” is on hold");
	});

	it("shows the hold in flight, with the reason locked and no second press", async () => {
		let answer;
		api.adminHold.mockReturnValue(new Promise((r) => (answer = r)));
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		expect(holdButton(w).attributes("data-loading")).toBe("1");
		expect(w.find(".stub-reason").attributes("disabled")).toBe("");
		await holdButton(w).trigger("click");
		expect(api.adminHold).toHaveBeenCalledTimes(1);
		// Escape or a click outside does not close it mid hold.
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		expect(w.emitted("update:modelValue")).toBeUndefined();
		answer({ ok: true, needs_apply: false, used_by_macros: used(0) });
		await flushPromises();
		expect(w.emitted("held")).toHaveLength(1);
	});

	it("keeps a refusal in the dialog, with the reason still typed, and tells the tab", async () => {
		api.adminHold.mockRejectedValue(new Error("You cannot hold your own skill."));
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe("You cannot hold your own skill.");
		expect(w.find(".stub-reason").element.value).toBe("Paused");
		expect(w.emitted("failed")).toEqual([[{ name: "s1" }]]);
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});

	it("keeps a reason that was typed and cancelled when the same skill's dialog opens again", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("Half a thought");
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.find(".stub-reason").element.value).toBe("Half a thought");
	});

	it("starts another skill's dialog empty", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("For the first one");
		await w.setProps({ modelValue: false });
		await w.setProps({ name: "s2", skillName: "other", modelValue: true });
		await flushPromises();
		expect(w.find(".stub-reason").element.value).toBe("");
	});

	it("clears the reason once the hold is made", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.find(".stub-reason").element.value).toBe("");
	});

	it("clears an old refusal when it opens again", async () => {
		api.adminHold.mockRejectedValueOnce(new Error("Nope"));
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(w.find(".stub-error").exists()).toBe(false);
	});
});
