import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// Banner pulls in frappe-ui, which does not load under vitest: a stand-in that
// renders what it is given.
vi.mock("./Banner.vue", () => ({
	default: {
		props: ["title", "message", "type"],
		template: "<div>{{ title }} {{ message }}<slot /></div>",
	},
}));

import ActionError from "./ActionError.vue";

describe("ActionError shows the person's words for a refusal", () => {
	it("renders error.person_message, not the model-facing message", () => {
		const w = mount(ActionError, {
			props: {
				error: {
					code: "sensitive_refused",
					message:
						"... Do not retry it with another tool; tell the user to make it in Desk.",
					person_message: "This change cannot be made from chat. Make it in Desk.",
					hint: "Make this change in Desk, where the whole record can be reviewed.",
				},
			},
		});
		expect(w.text()).toContain("This change cannot be made from chat. Make it in Desk.");
		expect(w.text()).not.toContain("another tool");
		expect(w.text()).toContain("where the whole record can be reviewed");
	});
});

describe("ActionError failure reference", () => {
	it("shows the reference when the failed envelope carries one", () => {
		const w = mount(ActionError, {
			props: { error: { message: "Failed", reference: "pa-5" } },
		});
		expect(w.text()).toContain("Reference:");
		expect(w.find("code").text()).toBe("pa-5");
	});

	it("none for a draft-panel failure (no confirmation row) or a plain string", () => {
		expect(
			mount(ActionError, { props: { error: { message: "Failed" } } }).text()
		).not.toContain("Reference:");
		expect(mount(ActionError, { props: { error: "boom" } }).text()).not.toContain(
			"Reference:"
		);
	});
});
