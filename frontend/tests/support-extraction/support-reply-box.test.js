import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// SupportReplyBox wraps the real SupportComposer, which pulls frappe-ui's
// TextEditor (TipTap) — stub the frappe-ui pieces + the editor CSS, same as the
// composer suite. withinSize stays real (toasts via the mocked toast).
vi.mock("frappe-ui/editor-style.css", () => ({}));
vi.mock("frappe-ui", () => ({
	TextEditor: {
		name: "TextEditor",
		props: [
			"content",
			"autofocus",
			"fixedMenu",
			"uploadFunction",
			"placeholder",
			"editorClass",
		],
		template: "<div class='editor' />",
	},
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading"],
		template: "<button :disabled='disabled'>{{ label }}</button>",
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<i />" },
	toast: { info: vi.fn(), error: vi.fn() },
}));

import SupportReplyBox from "@/components/support/SupportReplyBox.vue";
import SupportComposer from "@/components/support/SupportComposer.vue";

const mountBox = (props = {}) => mount(SupportReplyBox, { props: { expanded: false, ...props } });
const bar = (w) => w.find('[data-test="reply-bar"]');
const composer = (w) => w.findComponent(SupportComposer);

describe("SupportReplyBox — collapsed", () => {
	it("shows the one-line bar and mounts NO editor (the whole point: simple input, lazy TipTap)", () => {
		const w = mountBox({ placeholder: "Reply to Aerele Support…" });
		expect(bar(w).exists()).toBe(true);
		expect(bar(w).text()).toContain("Reply to Aerele Support…");
		// Perf + UX contract: the heavy composer/editor is NOT constructed while collapsed.
		expect(composer(w).exists()).toBe(false);
		expect(w.find(".editor").exists()).toBe(false);
	});

	it("requests expansion (v-model:expanded) when the bar is clicked", async () => {
		const w = mountBox();
		await bar(w).trigger("click");
		expect(w.emitted("update:expanded")[0]).toEqual([true]);
	});

	it("shows the disclaimer while collapsed (it's decision-relevant before expanding)", () => {
		const w = mountBox({ disclaimer: "Replying reopens this ticket." });
		expect(w.text()).toContain("Replying reopens this ticket.");
		expect(composer(w).exists()).toBe(false); // still no editor
	});
});

describe("SupportReplyBox — expanded", () => {
	it("mounts the composer with autofocus and no bar", () => {
		const w = mountBox({ expanded: true });
		expect(bar(w).exists()).toBe(false);
		const c = composer(w);
		expect(c.exists()).toBe(true);
		expect(c.props("autofocus")).toBe(true);
	});

	it("forwards the host props to the composer", () => {
		const w = mountBox({
			expanded: true,
			modelValue: "<p>hi</p>",
			pending: [{ key: "a", file_name: "x.png" }],
			canSubmit: true,
			loading: true,
			submitLabel: "Send",
			placeholder: "Reply…",
			disclaimer: "Replying reopens this ticket.",
		});
		const c = composer(w);
		expect(c.props("modelValue")).toBe("<p>hi</p>");
		expect(c.props("pending")).toHaveLength(1);
		expect(c.props("canSubmit")).toBe(true);
		expect(c.props("loading")).toBe(true);
		expect(c.props("submitLabel")).toBe("Send");
		expect(c.props("placeholder")).toBe("Reply…");
		// The wrapper owns the disclaimer (renders it once, in both states), so it
		// must NOT also hand it to the composer — else it double-renders when expanded.
		expect(w.text()).toContain("Replying reopens this ticket.");
		expect(c.props("disclaimer")).toBe("");
	});

	it("re-emits the composer's four events unchanged", () => {
		const w = mountBox({ expanded: true });
		const c = composer(w);
		c.vm.$emit("update:modelValue", "<p>typed</p>");
		c.vm.$emit("submit");
		c.vm.$emit("files-added", [{ name: "f.png" }]);
		c.vm.$emit("remove-attachment", 2);
		expect(w.emitted("update:modelValue")[0]).toEqual(["<p>typed</p>"]);
		expect(w.emitted("submit")).toHaveLength(1);
		expect(w.emitted("files-added")[0][0]).toEqual([{ name: "f.png" }]);
		expect(w.emitted("remove-attachment")[0]).toEqual([2]);
	});
});
