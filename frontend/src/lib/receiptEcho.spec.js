import { describe, expect, it } from "vitest";
import { isReceiptEcho } from "./receiptEcho";
import { receiptView } from "./actionSummary";

const chip = (over = {}) => ({
	role: "tool",
	tool_name: "update_doc",
	tool_args: JSON.stringify({ doctype: "Customer", name: "Grant Plastics Ltd" }),
	tool_result: JSON.stringify({
		ok: true,
		data: { doctype: "Customer", name: "Grant Plastics Ltd" },
	}),
	action_outcome: "confirmed",
	...over,
});
const say = (content, over = {}) => ({ role: "assistant", content, ...over });

describe("isReceiptEcho", () => {
	it("hides the assistant row that repeats the confirmed chip", () => {
		expect(isReceiptEcho(say("Updated Customer Grant Plastics Ltd."), chip())).toBe(true);
	});
	it("hides the create-and-submit wording when the chip says the same", () => {
		const c = chip({
			tool_name: "create_doc",
			tool_result: JSON.stringify({
				ok: true,
				data: { doctype: "Customer", name: "Grant Plastics Ltd", docstatus: 1 },
			}),
		});
		expect(receiptView("create_doc", {}, JSON.parse(c.tool_result), "confirmed").title).toBe(
			"Created and submitted Customer Grant Plastics Ltd"
		);
		expect(isReceiptEcho(say("Created and submitted Customer Grant Plastics Ltd."), c)).toBe(
			true
		);
	});
	it("keeps the submitted wording when the chip only says Created", () => {
		const c = chip({ tool_name: "create_doc" });
		expect(receiptView("create_doc", {}, JSON.parse(c.tool_result), "confirmed").title).toBe(
			"Created Customer Grant Plastics Ltd"
		);
		expect(isReceiptEcho(say("Created and submitted Customer Grant Plastics Ltd."), c)).toBe(
			false
		);
	});
	it("still hides a plain create echo", () => {
		const c = chip({ tool_name: "create_doc" });
		expect(isReceiptEcho(say("Created Customer Grant Plastics Ltd."), c)).toBe(true);
	});
	it("shows a different reply after the chip", () => {
		expect(isReceiptEcho(say("Done. Anything else?"), chip())).toBe(false);
	});
	it("shows the echo when it does not directly follow its chip", () => {
		const text = say("Updated Customer Grant Plastics Ltd.");
		expect(isReceiptEcho(text, { role: "user", content: "x" })).toBe(false);
		expect(isReceiptEcho(text, undefined)).toBe(false);
		expect(isReceiptEcho(text, chip({ action_outcome: "discarded" }))).toBe(false);
	});
	it("keeps a reply that adds the trigger note", () => {
		const t = say(
			"Updated Customer Grant Plastics Ltd. It is paused. Turn it on from Triggers."
		);
		expect(isReceiptEcho(t, chip())).toBe(false);
	});
	it("never hides an errored or streaming reply", () => {
		expect(
			isReceiptEcho(say("Updated Customer Grant Plastics Ltd.", { streaming: 1 }), chip())
		).toBe(false);
	});
});
