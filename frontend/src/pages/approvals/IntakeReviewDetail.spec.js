import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "placeholder", "modelValue", "loading"],
		emits: ["update:modelValue", "update:query"],
		template: '<input class="ac" :placeholder="placeholder" />',
	},
	Button: {
		props: ["label", "disabled", "loading", "theme", "variant"],
		emits: ["click"],
		template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FormControl: {
		props: ["label", "type", "modelValue", "options"],
		emits: ["update:modelValue"],
		template:
			'<label class="fc" :data-type="type">{{ label }}<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /></label>',
	},
	toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api", () => ({
	decideApproval: vi.fn(),
	searchLink: vi.fn(async () => []),
}));

import { toast } from "frappe-ui";
import * as api from "@/api";
import IntakeReviewDetail from "./IntakeReviewDetail.vue";

const item = (over) => ({
	key: "items.3.qty",
	field: "qty",
	row: 3,
	label: "Qty",
	fieldtype: "Float",
	options: "",
	answerable: true,
	reason: "Qty, row 3: unreadable",
	candidates: [],
	...over,
});
const approval = (missing, over = {}) => ({
	name: "AR-1",
	intake: {
		missing,
		read: [{ label: "Title", value: "Sheet 1" }],
		table: { label: "Items", columns: ["Item Name", "Qty"], rows: [["bolt", "2"]] },
		file: { file_url: "/private/files/a.png", file_name: "a.png", is_image: true },
		...over,
	},
});

let onDecided;
const mountWith = (missing, over) =>
	mount(IntakeReviewDetail, { props: { approval: approval(missing, over), onDecided } });
const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

describe("IntakeReviewDetail", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		onDecided = vi.fn();
	});

	it("renders one input per missing field", () => {
		const w = mountWith([
			item(),
			item({
				key: "customer",
				field: "customer",
				row: null,
				label: "Customer",
				fieldtype: "Link",
				options: "Customer",
				reason: "Customer Nobody: not found",
				candidates: ["ACME"],
			}),
			item({
				key: "title",
				field: "title",
				row: null,
				label: "Title",
				fieldtype: "Data",
				reason: "Title: unreadable",
			}),
		]);
		expect(w.findAll(".fc").length).toBe(2);
		expect(w.find(".fc[data-type='number']").text()).toContain("Qty");
		expect(w.findAll(".ac").length).toBe(1);
		expect(w.text()).toContain("Qty, row 3: unreadable");
		expect(w.text()).toContain("Closest");
		expect(w.find("img").attributes("src")).toBe("/private/files/a.png");
		expect(w.text()).toContain("Sheet 1");
		expect(w.text()).toContain("bolt");
	});

	it("keeps Approve off until every needed value has an answer", async () => {
		const w = mountWith([item()]);
		expect(button(w, "Approve").element.disabled).toBe(true);
		await w.find(".fc input").setValue("7");
		expect(button(w, "Approve").element.disabled).toBe(false);
	});

	it("sends answers as json on approve", async () => {
		api.decideApproval.mockResolvedValue({ ok: true, status: "Approved" });
		const w = mountWith([
			item(),
			item({
				key: "customer",
				field: "customer",
				row: null,
				label: "Customer",
				fieldtype: "Link",
				options: "Customer",
				reason: "Customer: unreadable",
				candidates: ["ACME"],
			}),
		]);
		await w.find(".fc input").setValue("7");
		await button(w, "ACME").trigger("click");
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(api.decideApproval).toHaveBeenCalledTimes(1);
		const [name, decision, approve] = api.decideApproval.mock.calls[0];
		expect([name, approve]).toEqual(["AR-1", 1]);
		expect(JSON.parse(decision)).toEqual({ "items.3.qty": 7, customer: "ACME" });
		expect(onDecided).toHaveBeenCalledWith({ ok: true, status: "Approved" }, "AR-1");
	});

	it("rejects with an empty json object", async () => {
		api.decideApproval.mockResolvedValue({ ok: true, status: "Rejected" });
		const w = mountWith([item()]);
		await button(w, "Reject").trigger("click");
		await flushPromises();
		expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "{}", 0);
		expect(onDecided).toHaveBeenCalled();
	});

	it("shows the server's refusal and stays open", async () => {
		api.decideApproval.mockRejectedValue(new Error("Still needed: Qty, row 3 (not a number)"));
		const w = mountWith([item()]);
		await w.find(".fc input").setValue("lots");
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(w.find("[role=alert]").exists()).toBe(true);
		expect(onDecided).not.toHaveBeenCalled();
		expect(toast.error).toHaveBeenCalled();
	});

	it("explains a field it cannot ask for", () => {
		const w = mountWith([
			item({ answerable: false, key: "items", row: null, reason: "Items: no rows read" }),
		]);
		expect(w.text()).toContain("Items: no rows read");
		expect(w.findAll(".fc").length).toBe(0);
		expect(button(w, "Approve").element.disabled).toBe(true);
	});
});
