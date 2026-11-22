import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: `<span class="badge">{{ label }}</span>` },
	FeatherIcon: { template: "<i />" },
	FormControl: {
		props: ["modelValue", "placeholder"],
		emits: ["update:modelValue"],
		template: `<input class="search" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
}));

import UserAgentToolPicker from "./UserAgentToolPicker.vue";

const groups = [
	{
		app: "frappe",
		label: "Records and reports",
		tools: [
			{ id: "get_list", label: "Get list", kind: "read" },
			{ id: "create_doc", label: "Create doc", kind: "write" },
		],
	},
	{
		app: "erpnext",
		label: "ERPNext",
		tools: [{ id: "get_stock_balance", label: "Get stock balance", kind: "read" }],
	},
];

const mountPicker = (props = {}) =>
	mount(UserAgentToolPicker, { props: { groups, modelValue: [], maxTools: 2, ...props } });
const rows = (w) => w.findAll('[role="checkbox"]');

describe("UserAgentToolPicker", () => {
	it("lists the tools grouped by app, marking the ones that write", () => {
		const w = mountPicker();
		expect(w.text()).toContain("Records and reports");
		expect(w.text()).toContain("ERPNext");
		expect(rows(w)).toHaveLength(3);
		expect(w.findAll(".badge").map((b) => b.text())).toEqual(["Writes"]);
	});

	it("searches by label or id and drops a group with no match", async () => {
		const w = mountPicker();
		await w.get(".search").setValue("stock");
		expect(rows(w)).toHaveLength(1);
		expect(w.text()).not.toContain("Records and reports");
		await w.get(".search").setValue("zzz");
		expect(w.text()).toContain("No tools match your search.");
	});

	it("emits the picked ids and shows the count against the maximum", async () => {
		const w = mountPicker({ modelValue: ["get_list"] });
		expect(w.text()).toContain("1 of 2 tools picked");
		await rows(w)[1].trigger("click");
		expect(w.emitted("update:modelValue")[0][0]).toEqual(["get_list", "create_doc"]);
		await rows(w)[0].trigger("click");
		expect(w.emitted("update:modelValue")[1][0]).toEqual([]);
	});

	it("stops adding at the maximum but still lets a picked tool go", async () => {
		const w = mountPicker({ modelValue: ["get_list", "create_doc"] });
		const third = rows(w)[2];
		expect(third.attributes("disabled")).toBeDefined();
		expect(rows(w)[0].attributes("disabled")).toBeUndefined();
	});

	it("says it is loading", () => {
		const w = mountPicker({ groups: [], loading: true });
		expect(w.text()).toContain("Loading tools...");
	});
});
