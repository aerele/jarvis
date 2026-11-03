import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template:
			'<div><button @click="$emit(\'update:modelValue\', false)">Close</button><slot name="body-header" /><slot name="body-content" /></div>',
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("reka-ui", () => ({ DialogTitle: { template: "<h2><slot /></h2>" } }));
vi.mock("@/pages/approvals/SheetDetail.vue", () => ({
	default: {
		name: "SheetDetail",
		props: ["name", "showOpenChat", "onChanged", "onDecided"],
		template: "<div>{{ name }}</div>",
	},
}));
import FileBoxSheetReview from "./FileBoxSheetReview.vue";
import SheetDetail from "@/pages/approvals/SheetDetail.vue";

describe("the in-chat native sheet review session", () => {
	it("opens the complete editor and passes its captured session on every callback", async () => {
		const session = { id: 1, conversation: "A", name: "SH-1" };
		const onChanged = vi.fn();
		const onClosed = vi.fn();
		const w = mount(FileBoxSheetReview, { props: { session, onChanged, onClosed } });
		const editor = w.findComponent(SheetDetail);
		expect(editor.props("name")).toBe("SH-1");
		expect(editor.props("showOpenChat")).toBe(false);
		editor.props("onChanged")({ ok: true });
		expect(onChanged).toHaveBeenCalledWith(session);
		editor.props("onDecided")({ status: "Executed" });
		expect(onClosed).toHaveBeenCalledWith(session);
		onClosed.mockClear();
		await w.get("button").trigger("click");
		expect(onClosed).toHaveBeenCalledOnce();
		expect(onClosed).toHaveBeenCalledWith(session);
		onClosed.mockClear();
		await w.get('[aria-label="Close review"]').trigger("click");
		expect(onClosed).toHaveBeenCalledWith(session);
		w.unmount();
	});

	it("keeps callbacks bound to the original session after a prop change", async () => {
		const session = { id: 1, conversation: "A", name: "SH-1" };
		const onChanged = vi.fn();
		const onClosed = vi.fn();
		const w = mount(FileBoxSheetReview, { props: { session, onChanged, onClosed } });
		const changed = w.findComponent(SheetDetail).props("onChanged");
		await w.setProps({ session: { id: 2, conversation: "B", name: "SH-2" } });
		changed();
		await w.get("button").trigger("click");
		expect(onChanged).toHaveBeenCalledWith(session);
		expect(onClosed).toHaveBeenCalledWith(session);
		expect(w.findComponent(SheetDetail).props("name")).toBe("SH-1");
		w.unmount();
	});
});
