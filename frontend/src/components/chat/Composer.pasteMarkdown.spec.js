import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Composer from "@/components/chat/Composer.vue";
import { MARKDOWN_TYPE } from "@/lib/richCopy";

// A reply copied with its Copy button carries its Markdown under MARKDOWN_TYPE;
// pasting it into the chat box must give that Markdown, not the plain text.

function paste(types) {
	const e = new Event("paste", { bubbles: true, cancelable: true });
	e.clipboardData = { files: [], items: [], getData: (t) => types[t] || "" };
	return e;
}

describe("Composer paste of a copied reply", () => {
	it("inserts the reply's Markdown at the caret", async () => {
		const w = mount(Composer, { props: { modelValue: "" }, attachTo: document.body });
		const ta = w.get("textarea").element;
		ta.value = "see: ";
		ta.setSelectionRange(5, 5);
		const e = paste({ [MARKDOWN_TYPE]: "**6** customers", "text/plain": "6 customers" });
		ta.dispatchEvent(e);
		expect(e.defaultPrevented).toBe(true);
		const updates = w.emitted("update:modelValue");
		expect(updates.at(-1)[0]).toBe("see: **6** customers");
		w.unmount();
	});

	it("leaves an ordinary text paste to the browser", () => {
		const w = mount(Composer, { props: { modelValue: "" } });
		const e = paste({ "text/plain": "hello" });
		w.get("textarea").element.dispatchEvent(e);
		expect(e.defaultPrevented).toBe(false);
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});
});
