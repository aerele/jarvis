import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Releasing an admin's hold. What matters: "and switch it back on" is offered only
 * for a skill shared with a role or everyone, starts unticked every time, and is
 * sent only when it was offered and ticked; a private skill says its owner switches
 * it on; nothing says a release re-enables writing without asking.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "loading", "disabled"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" @click="$emit('click')">{{ label }}</button>`,
	},
	Checkbox: {
		name: "Checkbox",
		props: ["label", "modelValue", "disabled"],
		emits: ["update:modelValue"],
		template: `<label class="stub-checkbox"><input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)" />{{ label }}</label>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><slot name="body-title"><h4>{{ options && options.title }}</h4></slot><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span class="stub-error">{{ message }}</span>`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

const api = vi.hoisted(() => ({ adminRelease: vi.fn() }));
vi.mock("@/api/skillsAdmin", () => api);

import { toast } from "frappe-ui";
import SkillReleaseDialog from "./SkillReleaseDialog.vue";

function open(props = {}) {
	return mount(SkillReleaseDialog, {
		props: {
			modelValue: true,
			name: "s1",
			skillName: "month-end",
			ownerLabel: "Asha Rao (asha@example.test)",
			shared: true,
			...props,
		},
	});
}
const release = (w) => w.findAll("button.stub-button").find((b) => b.text() === "Release");
const tick = (w) => w.find(".stub-checkbox input");

beforeEach(() => {
	vi.clearAllMocks();
	api.adminRelease.mockResolvedValue({ ok: true, restored: false, needs_apply: false });
});

describe("SkillReleaseDialog", () => {
	it("offers to switch a shared skill back on, unticked, and says release does not allow writes", () => {
		const w = open();
		expect(w.find("h3").text()).toBe("Release the hold: month-end");
		expect(w.find(".stub-checkbox").text()).toBe("and switch it back on");
		expect(tick(w).element.checked).toBe(false);
		expect(w.text()).toContain("Releasing does not let it write without asking");
	});

	it("offers it for a role skill and a company skill alike (both arrive as shared)", () => {
		expect(open({ shared: true }).find(".stub-checkbox").exists()).toBe(true);
	});

	it("does not offer it for a private skill, and says its owner switches it on", () => {
		const w = open({ shared: false });
		expect(w.find(".stub-checkbox").exists()).toBe(false);
		expect(w.find(".jv-skill-release-owner-note").text()).toBe(
			"It stays off until its owner switches it on."
		);
	});

	it("releases without switching on when the box is left alone", async () => {
		const w = open();
		await release(w).trigger("click");
		await flushPromises();
		expect(api.adminRelease).toHaveBeenCalledWith("s1", false);
		expect(toast.success).toHaveBeenCalledWith("Released the hold on “month-end”");
		expect(w.emitted("released")).toEqual([[{ name: "s1", needs_apply: false }]]);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("releases and switches back on when ticked, and says so", async () => {
		api.adminRelease.mockResolvedValue({ ok: true, restored: true, needs_apply: true });
		const w = open();
		await tick(w).setValue(true);
		await release(w).trigger("click");
		await flushPromises();
		expect(api.adminRelease).toHaveBeenCalledWith("s1", true);
		expect(toast.success).toHaveBeenCalledWith(
			"Released the hold on “month-end” and switched it back on"
		);
		expect(w.emitted("released")).toEqual([[{ name: "s1", needs_apply: true }]]);
	});

	it("never sends a restore for a private skill", async () => {
		const w = open({ shared: false });
		await release(w).trigger("click");
		await flushPromises();
		expect(api.adminRelease).toHaveBeenCalledWith("s1", false);
	});

	it("starts unticked every time it opens", async () => {
		const w = open();
		await tick(w).setValue(true);
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		expect(tick(w).element.checked).toBe(false);
	});

	it("keeps a refusal in the dialog and tells the tab", async () => {
		api.adminRelease.mockRejectedValue(new Error("This skill is not on hold."));
		const w = open();
		await release(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe("This skill is not on hold.");
		expect(w.emitted("failed")).toEqual([[{ name: "s1" }]]);
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});

	it("will not close or send twice while it is in flight", async () => {
		let answer;
		api.adminRelease.mockReturnValue(new Promise((r) => (answer = r)));
		const w = open();
		await release(w).trigger("click");
		expect(release(w).attributes("data-loading")).toBe("1");
		await release(w).trigger("click");
		expect(api.adminRelease).toHaveBeenCalledTimes(1);
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		expect(w.emitted("update:modelValue")).toBeUndefined();
		answer({ ok: true, restored: false, needs_apply: false });
		await flushPromises();
		expect(w.emitted("released")).toHaveLength(1);
	});
});
