import { mount } from "@vue/test-utils";
import { describe, it, expect } from "vitest";
import Composer from "./Composer.vue";

describe("read-only composer presentation", () => {
	it("uses the normal composer frame without editable or upload inputs", () => {
		const w = mount(Composer, { props: { readonly: true, placeholder: "View only" } });
		expect(w.find(".jv-composer").exists()).toBe(true);
		expect(w.find("textarea,input,[contenteditable=true]").exists()).toBe(false);
		expect(w.get('[role="textbox"]').attributes("aria-disabled")).toBe("true");
		expect(w.text()).toContain("View only");
		for (const b of w.findAll("button")) expect(b.attributes("disabled")).toBeDefined();
		w.unmount();
	});
	it("prevents a dropped file navigating the browser and never emits an upload", () => {
		const w = mount(Composer, { props: { readonly: true } });
		const e = new Event("drop", { bubbles: true, cancelable: true });
		Object.defineProperty(e, "dataTransfer", {
			value: { files: [new File(["private"], "private.txt")] },
		});
		w.get(".jv-composer").element.dispatchEvent(e);
		expect(e.defaultPrevented).toBe(true);
		expect(w.emitted("files-added")).toBeUndefined();
		w.unmount();
	});
	it("does not submit even from a synthetic click or Enter", async () => {
		const w = mount(Composer, {
			props: { readonly: true, modelValue: "An unsent draft", canSend: true },
		});
		w.get(".jv-sendbtn").element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
		await w.get('[role="textbox"]').trigger("keydown", { key: "Enter" });
		expect(w.emitted("submit")).toBeUndefined();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		w.unmount();
	});
	it("keeps a slot picker inert and hides pending private attachments", () => {
		let pick;
		const w = mount(Composer, {
			props: { readonly: true, attachments: [{ file_name: "private draft.pdf" }] },
			slots: {
				"left-toolbar": ({ pickFiles }) => {
					pick = pickFiles;
					return "";
				},
			},
		});
		pick();
		expect(w.find('input[type="file"]').exists()).toBe(false);
		expect(w.text()).not.toContain("private draft.pdf");
		expect(w.emitted()).toEqual({});
		w.unmount();
	});
	it("restores an existing draft when write access returns", async () => {
		const w = mount(Composer, { props: { modelValue: "Keep this draft" } });
		expect(w.get("textarea").element.value).toBe("Keep this draft");
		await w.setProps({ readonly: true });
		expect(w.find("textarea").exists()).toBe(false);
		await w.setProps({ readonly: false });
		expect(w.get("textarea").element.value).toBe("Keep this draft");
		await w.get("textarea").trigger("keydown", { key: "Enter" });
		expect(w.emitted("submit")).toHaveLength(1);
		w.unmount();
	});
});
