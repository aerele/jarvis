import { describe, expect, it, vi } from "vitest";

// Stubs shaped like frappe-ui's pickers (0.1.x): the root is a Popover that keeps
// no attrs (inheritAttrs:false, only class/style), and the <input> inside is drawn
// by TextInput with its OWN class list, which it rewrites when `disabled` changes.
// A plain `<input />` stub would hide the bug: attrs would fall through onto it.
vi.mock("frappe-ui", () => {
	const picker = (name) => ({
		name,
		inheritAttrs: false,
		props: ["modelValue", "disabled", "placeholder"],
		emits: ["update:modelValue"],
		template: `<div class="popover-root"><div class="textinput-root"><input
			:class="disabled ? 'ti ti-disabled' : 'ti'"
			:disabled="disabled"
			:value="modelValue"
			@input="$emit('update:modelValue', $event.target.value)"
		/></div></div>`,
	});
	return {
		DatePicker: picker("DatePicker"),
		DateTimePicker: picker("DateTimePicker"),
		TimePicker: picker("TimePicker"),
	};
});

import { mount } from "@vue/test-utils";
import DateControl from "./DateControl.vue";

const mountCtl = (props = {}, attrs = {}) =>
	mount(DateControl, { props: { kind: "date", ...props }, attrs });

describe("DateControl puts the caller's attributes on the real input", () => {
	it("moves id and aria-* onto the input, not the wrapper or the popover", () => {
		const w = mountCtl(
			{ ariaLabel: "Posting Date" },
			{ id: "fld-posting", "aria-describedby": "fld-posting-was", "aria-required": "true" }
		);
		const input = w.find("input");
		expect(input.attributes("id")).toBe("fld-posting");
		expect(input.attributes("aria-label")).toBe("Posting Date");
		expect(input.attributes("aria-describedby")).toBe("fld-posting-was");
		expect(input.attributes("aria-required")).toBe("true");
		expect(w.find(".jv-datectl").attributes("id")).toBeUndefined();
		expect(w.find(".popover-root").attributes("id")).toBeUndefined();
	});

	it("does the same for the datetime and time pickers", () => {
		for (const kind of ["datetime", "time"]) {
			const input = mountCtl({ kind }, { id: `f-${kind}`, "aria-required": "true" }).find(
				"input"
			);
			expect([input.attributes("id"), input.attributes("aria-required")]).toEqual([
				`f-${kind}`,
				"true",
			]);
		}
	});

	it("drops an attribute the caller stops passing", async () => {
		const w = mountCtl({}, { id: "f1", "aria-required": "true" });
		await w.setProps({ "aria-required": undefined });
		expect(w.find("input").attributes("aria-required")).toBeUndefined();
		expect(w.find("input").attributes("id")).toBe("f1");
	});
});

describe("DateControl gives the input its neighbours' class", () => {
	it("a card field gets .jv-action-input, so .missing / .invalid / .changed reach it", () => {
		const input = mountCtl({ look: "card" }).find("input");
		expect(input.classes()).toContain("jv-action-input");
		expect(input.classes()).toContain("ti"); // the picker's own class is kept
	});

	it("a question-card field gets .jv-ask-field, plus any inputClass", () => {
		const input = mountCtl({ look: "ask", inputClass: "extra-a extra-b" }).find("input");
		expect(input.classes()).toEqual(
			expect.arrayContaining(["jv-ask-field", "extra-a", "extra-b"])
		);
		expect(input.classes()).not.toContain("jv-action-input");
	});

	it("keeps the class after the picker rewrites its own on a disabled toggle", async () => {
		const w = mountCtl({ look: "card", disabled: false });
		await w.setProps({ disabled: true });
		const input = w.find("input");
		expect(input.classes()).toEqual(
			expect.arrayContaining(["ti-disabled", "jv-action-input"])
		);
		expect(input.attributes("disabled")).toBeDefined();
	});
});

describe("DateControl speaks the native inputs' value strings", () => {
	it("passes disabled to the picker", () => {
		const w = mountCtl({ disabled: true });
		expect(w.findComponent({ name: "DatePicker" }).props("disabled")).toBe(true);
	});

	it("date: YYYY-MM-DD in, YYYY-MM-DD out", async () => {
		const w = mountCtl({ modelValue: "2026-03-04" });
		expect(w.findComponent({ name: "DatePicker" }).props("modelValue")).toBe("2026-03-04");
		await w.find("input").setValue("2026-03-05 00:00:00");
		expect(w.emitted("update:modelValue").at(-1)).toEqual(["2026-03-05"]);
	});

	it("datetime: the card's YYYY-MM-DDTHH:mm round-trips through the picker's format", async () => {
		const w = mountCtl({ kind: "datetime", modelValue: "2026-03-04T10:30" });
		expect(w.findComponent({ name: "DateTimePicker" }).props("modelValue")).toBe(
			"2026-03-04 10:30:00"
		);
		await w.find("input").setValue("2026-03-05 11:00:00");
		expect(w.emitted("update:modelValue").at(-1)).toEqual(["2026-03-05T11:00"]);
	});

	it("time: HH:mm, seconds dropped", async () => {
		const w = mountCtl({ kind: "time", modelValue: "09:15:00" });
		expect(w.findComponent({ name: "TimePicker" }).props("modelValue")).toBe("09:15");
		await w.find("input").setValue("18:45:30");
		expect(w.emitted("update:modelValue").at(-1)).toEqual(["18:45"]);
	});

	it("empty stays empty", async () => {
		const w = mountCtl({ modelValue: "" });
		await w.find("input").setValue("");
		expect(w.emitted("update:modelValue").at(-1)).toEqual([""]);
	});
});
