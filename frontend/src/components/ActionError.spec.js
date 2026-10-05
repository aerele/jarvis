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
