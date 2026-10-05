import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import FailureReference from "./FailureReference.vue";
// The phone's copy (pwa/src/components) has no component runner of its own; mounted here.
import PwaFailureReference from "../../../pwa/src/components/FailureReference.vue";

function setClipboard(writeText) {
	Object.defineProperty(globalThis.navigator, "clipboard", {
		value: writeText ? { writeText } : undefined,
		configurable: true,
	});
}

afterEach(() => setClipboard(null));

for (const [label, Component] of [
	["desktop", FailureReference],
	["phone", PwaFailureReference],
]) {
	describe(`${label} FailureReference`, () => {
		it("shows the reference in monospace with a labelled copy button", () => {
			const w = mount(Component, { props: { id: "pa-0042" } });
			expect(w.text()).toContain("Reference:");
			expect(w.find("code").text()).toBe("pa-0042");
			const btn = w.find("button");
			expect(btn.attributes("aria-label")).toBe("Copy reference pa-0042");
			expect(btn.text()).toBe("Copy");
		});

		it("copies it and says so", async () => {
			const writeText = vi.fn().mockResolvedValue(undefined);
			setClipboard(writeText);
			const w = mount(Component, { props: { id: "pa-0042" } });
			await w.find("button").trigger("click");
			await flushPromises();
			expect(writeText).toHaveBeenCalledWith("pa-0042");
			expect(w.find('[role="status"]').text()).toBe("Copied.");
		});

		it("falls back to selecting the text when the clipboard refuses", async () => {
			setClipboard(vi.fn().mockRejectedValue(new Error("denied")));
			const w = mount(Component, { props: { id: "pa-0042" }, attachTo: document.body });
			await w.find("button").trigger("click");
			await flushPromises();
			expect(w.find('[role="status"]').text()).toBe(
				"Selected. Copy it with your device's copy command."
			);
			expect(String(window.getSelection())).toBe("pa-0042");
			w.unmount();
		});

		it("renders nothing without an id", () => {
			const w = mount(Component, { props: { id: "" } });
			expect(w.text()).toBe("");
			expect(w.find("button").exists()).toBe(false);
		});
	});
}
