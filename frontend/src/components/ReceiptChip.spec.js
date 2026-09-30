import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import ReceiptChip from "./ReceiptChip.vue";

// An uncarded write with no macro or skill name: a request-scoped "confirm all"
// in a normal chat, or any covered write in an auto-mode chat (#581).
const autoApplied = {
	role: "tool",
	tool_name: "create_doc",
	tool_args: JSON.stringify({ doctype: "ToDo", doc: { description: "Call Acme" } }),
	tool_result: JSON.stringify({ ok: true, name: "TD-0001", doctype: "ToDo" }),
	action_outcome: "auto_applied",
};

describe("ReceiptChip auto-applied provenance", () => {
	it("says the user approved the request in a normal chat", () => {
		const w = mount(ReceiptChip, { props: { message: autoApplied } });
		expect(w.text()).toContain("you approved this request");
	});

	it("drops the confirm-all line in an auto-mode chat", () => {
		const w = mount(ReceiptChip, { props: { message: autoApplied, autoMode: true } });
		expect(w.text()).not.toContain("you approved this request");
		expect(w.find(".jv-receipt-armed").exists()).toBe(false);
	});

	it("keeps a macro's name in an auto-mode chat", () => {
		const w = mount(ReceiptChip, {
			props: { message: { ...autoApplied, armed_by_macro: "Daily close" }, autoMode: true },
		});
		expect(w.text()).toContain("Daily close");
	});
});
