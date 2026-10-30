import { describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// frappe-ui cannot load under vitest; this stands in for its Switch, which is a root
// element around one role="switch" button (real Chrome checks the real one).
vi.mock("frappe-ui", () => ({
	Switch: {
		props: ["modelValue", "disabled"],
		emits: ["update:modelValue"],
		template:
			'<div><button role="switch" :aria-checked="String(modelValue)" :disabled="disabled" @click="$emit(\'update:modelValue\', !modelValue)"></button></div>',
	},
}));

import ToggleRow from "./ToggleRow.vue";

// The settings staple draws its own title; the switch must still have a name of its own.
const nameOf = (button, root) =>
	(button.getAttribute("aria-labelledby") || "")
		.split(/\s+/)
		.map((id) => root.querySelector(`#${id}`)?.textContent.trim())
		.join(" ");

describe("ToggleRow", () => {
	it("names the switch by the row title, and describes it by the help", async () => {
		const w = mount(ToggleRow, {
			props: { title: "Show holiday touches", help: "Greetings on the welcome screen" },
			attachTo: document.body,
		});
		await flushPromises();
		const button = w.get('[role="switch"]').element;
		expect(nameOf(button, w.element)).toBe("Show holiday touches");
		const described = document.getElementById(button.getAttribute("aria-describedby"));
		expect(described.textContent.trim()).toBe("Greetings on the welcome screen");
		w.unmount();
	});

	it("a row without help has a name and no description", async () => {
		const w = mount(ToggleRow, { props: { title: "Show tool activity" } });
		await flushPromises();
		const button = w.get('[role="switch"]').element;
		expect(nameOf(button, w.element)).toBe("Show tool activity");
		expect(button.hasAttribute("aria-describedby")).toBe(false);
	});

	it("two rows in one page never share an id", async () => {
		// useId is unique per app, so both rows live in one.
		const w = mount({
			components: { ToggleRow },
			template:
				'<div><ToggleRow title="A" help="a" /><ToggleRow title="B" help="b" /></div>',
		});
		await flushPromises();
		const ids = w
			.findAll('[role="switch"]')
			.map((b) => b.element.getAttribute("aria-labelledby"));
		expect(new Set(ids).size).toBe(2);
		expect(ids.map((id) => w.element.querySelector(`#${id}`).textContent.trim())).toEqual([
			"A",
			"B",
		]);
	});

	it("follows a changed title and still toggles", async () => {
		const w = mount(ToggleRow, { props: { title: "Old", modelValue: false } });
		await flushPromises();
		await w.setProps({ title: "New" });
		expect(nameOf(w.get('[role="switch"]').element, w.element)).toBe("New");
		await w.get('[role="switch"]').trigger("click");
		expect(w.emitted("update:modelValue")?.[0]).toEqual([true]);
	});
});
