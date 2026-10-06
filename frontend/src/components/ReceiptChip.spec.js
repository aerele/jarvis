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

	it("says the user approved the request with the bolt icon in a normal chat", () => {
		const w = mount(ReceiptChip, { props: { message: autoApplied } });
		expect(w.find(".jv-receipt-automode").exists()).toBe(false);
	});

	it("drops the confirm-all line in an auto-mode chat", () => {
		const w = mount(ReceiptChip, { props: { message: autoApplied, autoMode: true } });
		expect(w.text()).not.toContain("you approved this request");
		expect(w.find(".jv-receipt-armed").exists()).toBe(false);
	});

	it("shows the auto mode toggle's icon on an auto-mode receipt", () => {
		const w = mount(ReceiptChip, { props: { message: autoApplied, autoMode: true } });
		expect(w.find(".jv-receipt-automode").exists()).toBe(true);
	});

	it("keeps a macro's name in an auto-mode chat", () => {
		const w = mount(ReceiptChip, {
			props: { message: { ...autoApplied, armed_by_macro: "Daily close" }, autoMode: true },
		});
		expect(w.text()).toContain("Daily close");
		expect(w.find(".jv-receipt-automode").exists()).toBe(false);
	});
});

describe("ReceiptChip failure reference", () => {
	const failed = {
		role: "tool",
		tool_name: "submit_doc",
		tool_args: JSON.stringify({ doctype: "Sales Order", name: "SO-1" }),
		tool_result: JSON.stringify({
			ok: false,
			error: {
				code: "InvalidArgumentError",
				message: "Credit limit crossed",
				reference: "pa-77",
			},
		}),
		action_outcome: "failed",
	};

	it("a failed chip shows the reference with a copy button", () => {
		const w = mount(ReceiptChip, { props: { message: failed } });
		expect(w.text()).toContain("Reference:");
		expect(w.find("code").text()).toBe("pa-77");
		expect(w.find('button[aria-label="Copy reference pa-77"]').exists()).toBe(true);
	});

	it("a partial chip shows it too", () => {
		const w = mount(ReceiptChip, {
			props: { message: { ...failed, action_outcome: "partial" } },
		});
		expect(w.find("code").text()).toBe("pa-77");
	});

	it("no reference on a confirmed chip, or when the bench sent none", () => {
		expect(mount(ReceiptChip, { props: { message: autoApplied } }).text()).not.toContain(
			"Reference:"
		);
		const bare = {
			...failed,
			tool_result: JSON.stringify({ ok: false, error: { message: "x" } }),
		};
		expect(mount(ReceiptChip, { props: { message: bare } }).text()).not.toContain(
			"Reference:"
		);
	});
});
