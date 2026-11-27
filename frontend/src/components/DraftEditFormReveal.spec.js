// The shared form adds a field for its mandatory rule when the person's edit makes it hold.
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick, reactive } from "vue";
import DraftEditForm from "./chat/DraftEditForm.vue";
import { panelField } from "../lib/docFields.js";

const meta = (fieldname, fieldtype, extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

describe("DraftEditForm reveals a rule-required field", () => {
	// Break: the form never looks again after the build (no field appears on the edit), or the
	// reveal retriggers itself (a field added, the watcher run again, without end).
	it("shows the field on the edit, with its star, and settles", async () => {
		const status = meta("status", "Select", { options: "Open\nCompleted" });
		const done = meta("completed_on", "Date", {
			mandatory_depends_on: 'eval:doc.status=="Completed"',
		});
		const model = reactive({
			verb: "create",
			fields: [panelField(status, "Open")],
			tables: [],
			ruleBase: {},
			ruleFields: [{ field: done, value: undefined }],
		});
		const w = mount(DraftEditForm, {
			props: {
				model,
				draftLink: { key: "", items: [], open: false, anchor: null },
				palette: {},
			},
		});
		expect(w.text()).not.toContain("completed_on");
		model.fields[0].value = "Completed";
		await nextTick();
		await nextTick();
		expect(model.fields.map((f) => f.fieldname)).toEqual(["status", "completed_on"]);
		expect(model.ruleFields).toEqual([]);
		expect(w.find(".jv-req").exists()).toBe(true);
	});
});
