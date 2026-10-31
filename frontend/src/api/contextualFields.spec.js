import { beforeEach, expect, it, vi } from "vitest";

vi.mock("frappe-ui", () => ({ call: vi.fn(async () => ({})) }));
import { call } from "frappe-ui";
import { getDoctypeFields } from "../api.js";

beforeEach(() => call.mockClear());

it("keeps parentless metadata requests unchanged", async () => {
	await getDoctypeFields("Item");
	expect(call).toHaveBeenCalledWith("jarvis.chat.api.get_doctype_fields", { doctype: "Item" });
});

it("sends the parent and table identity for a child field", async () => {
	await getDoctypeFields("Sales Invoice Item", "Sales Invoice", "items");
	expect(call).toHaveBeenCalledWith("jarvis.chat.api.get_doctype_fields", {
		doctype: "Sales Invoice Item",
		parent_doctype: "Sales Invoice",
		parentfield: "items",
	});
});
