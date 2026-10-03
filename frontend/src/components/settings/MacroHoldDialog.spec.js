import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The question an admin answers before putting another user's macro on hold: why.
 * What matters: no reason, no hold; a reason over the server's limit is refused
 * here first; the hold is sent with the reason as typed (trimmed); a refusal stays
 * in the dialog; names go into the toast escaped.
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
		template: `<div v-if="modelValue" class="stub-dialog"><h4>{{ options && options.title }}</h4><slot name="body-content" /><slot name="actions" /></div>`,
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
vi.mock("@/api/macrosAdmin", () => api);

import { toast } from "frappe-ui";
import MacroHoldDialog from "./MacroHoldDialog.vue";

function open(props = {}) {
	return mount(MacroHoldDialog, {
		props: {
			modelValue: true,
			name: "m1",
			macroName: "Month end",
			ownerLabel: "Asha Rao (asha@example.test)",
			...props,
		},
	});
}
const holdButton = (w) => w.findAll("button.stub-button").find((b) => b.text() === "Put on hold");

beforeEach(() => {
	vi.clearAllMocks();
	api.adminHold.mockResolvedValue({ ok: true, held: true, stopped_runs: 0 });
});

describe("MacroHoldDialog", () => {
	it("names the macro and its owner, and says what a hold does", () => {
		const w = open();
		expect(w.find("h4").text()).toBe("Put “Month end” on hold (Asha Rao (asha@example.test))");
		expect(w.text()).toContain("The macro stops running until an admin releases it");
		expect(w.text()).toContain("Its owner sees the reason on the macro.");
	});

	it("will not hold without a reason", async () => {
		const w = open();
		expect(holdButton(w).attributes("disabled")).toBe("");
		await w.find(".stub-reason").setValue("   ");
		expect(holdButton(w).attributes("disabled")).toBe("");
		await holdButton(w).trigger("click");
		expect(api.adminHold).not.toHaveBeenCalled();
	});

	it("refuses a reason over the server's limit before asking it", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("x".repeat(501));
		expect(holdButton(w).attributes("disabled")).toBe("");
		expect(w.text()).toContain("501 / 500");
		await w.find(".stub-reason").setValue("x".repeat(500));
		expect(holdButton(w).attributes("disabled")).toBeUndefined();
	});

	it("holds with the reason, says so, tells the pane and closes", async () => {
		api.adminHold.mockResolvedValue({ ok: true, held: true, stopped_runs: 2 });
		const w = open();
		await w.find(".stub-reason").setValue("  Sends too many emails  ");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHold).toHaveBeenCalledWith("m1", "Sends too many emails");
		expect(toast.success).toHaveBeenCalledWith("“Month end” is on hold; 2 runs stopped");
		expect(w.emitted("held")).toEqual([[{ name: "m1", stopped_runs: 2 }]]);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("shows the hold in flight, with the reason locked", async () => {
		let answer;
		api.adminHold.mockReturnValue(new Promise((r) => (answer = r)));
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		expect(holdButton(w).attributes("data-loading")).toBe("1");
		expect(w.find(".stub-reason").attributes("disabled")).toBe("");
		await holdButton(w).trigger("click");
		expect(api.adminHold).toHaveBeenCalledTimes(1);
		answer({ ok: true, held: true, stopped_runs: 0 });
		await flushPromises();
	});

	it("keeps a refusal in the dialog, and stays open", async () => {
		api.adminHold.mockRejectedValue(
			new Error("This is your own macro. Switch it off on the macro form instead.")
		);
		const w = open();
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toContain("This is your own macro.");
		expect(w.emitted("held")).toBeUndefined();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("puts the macro's name in the toast escaped", async () => {
		const w = open({ macroName: "<img src=x onerror=alert(1)>" });
		await w.find(".stub-reason").setValue("Paused");
		await holdButton(w).trigger("click");
		await flushPromises();
		expect(toast.success.mock.calls[0][0]).toBe(
			"“&lt;img src=x onerror=alert(1)&gt;” is on hold"
		);
	});

	it("caps the reason at the limit, and the count is read out as it changes", () => {
		const w = open();
		expect(w.find("textarea.stub-reason").attributes("maxlength")).toBe("500");
		const count = w.find(".jv-macro-hold-count");
		expect(count.attributes("aria-live")).toBe("polite");
		expect(count.text()).toBe("0 / 500");
	});

	it("starts empty each time it opens", async () => {
		const w = open();
		await w.find(".stub-reason").setValue("first");
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		expect(w.find(".stub-reason").element.value).toBe("");
	});
});
