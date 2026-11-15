import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

const push = vi.fn();
vi.mock("vue-router", () => ({
	useRoute: () => ({ params: { activity: "A1" } }),
	useRouter: () => ({ push }),
}));
vi.mock("frappe-ui", () => ({
	Badge: {
		props: ["label", "theme"],
		template: '<span class="badge" :data-theme="theme">{{ label }}</span>',
	},
	Breadcrumbs: {
		props: ["items"],
		template: '<nav class="crumbs">{{ items.map((i) => i.label).join(" / ") }}</nav>',
	},
	Button: {
		props: ["label", "variant"],
		emits: ["click"],
		template:
			'<button class="btn" :data-variant="variant" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<div><slot name="left-header" /></div>' },
}));
vi.mock("@/utils/datetime", () => ({ formatDate: (d) => String(d) }));
vi.mock("@/api/intake", () => ({ intakeDetail: vi.fn(), intakeRetry: vi.fn() }));

import { intakeDetail, intakeRetry } from "@/api/intake";
import IntakeDetail from "./IntakeDetail.vue";

const detail = (over = {}) => ({
	row: {
		name: "A1",
		creation: "2026-11-13 10:00:00",
		status: "Success",
		summary: "Draft ready: ToDo T1",
		file_name: "bill.png",
		source_label: "Folder: Bills",
		draft_doctype: "ToDo",
		draft_name: "T1",
		approval_request: "",
		draft_missing: false,
	},
	badge: "",
	file: { url: "/private/files/bill.png", is_image: true, is_pdf: false },
	spec: {
		target: "ToDo",
		fields: [
			{ fieldname: "description", label: "Description", fieldtype: "Data" },
			{ fieldname: "priority", label: "Priority", fieldtype: "Select" },
			{ fieldname: "date", label: "Due Date", fieldtype: "Date" },
		],
		table: { fieldname: "items", label: "Items", columns: [{ fieldname: "q", label: "Qty" }] },
	},
	reading: {
		values: { description: "Pay the bill", priority: "Urgent", date: "" },
		table_rows: [["3"]],
	},
	draft: {
		doctype: "ToDo",
		name: "T1",
		values: { description: "Pay the bill", priority: "", date: "" },
	},
	changes: { priority: "Not one of the options, left blank" },
	missing: ["date"],
	can_retry: false,
	can_review: false,
	...over,
});

async function mountPage(d) {
	intakeDetail.mockResolvedValue(d);
	const w = mount(IntakeDetail);
	await flushPromises();
	return w;
}
const labels = (w) => w.findAll("button").map((b) => b.text());

describe("IntakeDetail", () => {
	beforeEach(() => vi.clearAllMocks());

	it("shows the photo and one row per spec field", async () => {
		const w = await mountPage(detail());
		expect(w.find("[data-testid=intake-photo] img").attributes("src")).toBe(
			"/private/files/bill.png"
		);
		expect(w.find("[data-field=description]").text()).toContain("Pay the bill");
		expect(w.find("[data-field=date]").text()).toContain("Not found on the page");
		expect(w.find(".crumbs").text()).toBe("File Box / bill.png");
		expect(w.find("[data-testid=intake-state]").text()).toContain("Draft created: ToDo T1");
		expect(w.findAll("[data-testid=intake-row]")).toHaveLength(1);
		expect(w.find("[data-testid=intake-row]").text()).toContain("Qty");
	});

	it("notes where the draft differs and why", async () => {
		const w = await mountPage(detail());
		const note = w.find("[data-field=priority] [data-testid=intake-change]");
		expect(note.text()).toContain("On the draft: blank");
		expect(note.text()).toContain("Not one of the options");
		expect(w.find("[data-field=description] [data-testid=intake-change]").exists()).toBe(
			false
		);
	});

	it("hides the draft note when the draft cannot be read", async () => {
		const w = await mountPage(detail({ draft: null, changes: {} }));
		expect(w.find("[data-testid=intake-change]").exists()).toBe(false);
	});

	it("Success: Open draft is the primary action; no Retry or Open review", async () => {
		const w = await mountPage(detail());
		expect(labels(w)).toContain("Open draft");
		expect(labels(w)).not.toContain("Retry");
		expect(labels(w)).not.toContain("Open review");
		const open = w.findAll("button").find((b) => b.text() === "Open draft");
		expect(open.attributes("data-variant")).toBe("solid");
	});

	it("Failed with can_retry: reason first and Retry runs retry_intake", async () => {
		intakeRetry.mockResolvedValue({ ok: true });
		const w = await mountPage(
			detail({
				row: {
					...detail().row,
					status: "Failed",
					summary: "Reading the file failed.",
					draft_name: "",
					draft_doctype: "",
				},
				can_retry: true,
				draft: null,
			})
		);
		expect(w.find("[data-testid=intake-state]").text()).toContain("Why it failed");
		expect(w.find("[data-testid=intake-state]").text()).toContain("Reading the file failed.");
		expect(labels(w)).not.toContain("Open draft");
		await w
			.findAll("button")
			.find((b) => b.text() === "Retry")
			.trigger("click");
		expect(intakeRetry).toHaveBeenCalledWith("A1");
	});

	it("Needs review: names the missing fields and Open review goes to the board", async () => {
		const w = await mountPage(
			detail({
				row: {
					...detail().row,
					status: "Needs Review",
					draft_name: "",
					draft_doctype: "",
					approval_request: "AR1",
				},
				can_review: true,
				draft: null,
			})
		);
		expect(w.find("[data-testid=intake-state]").text()).toContain(
			"Not found on the page: Due Date"
		);
		await w
			.findAll("button")
			.find((b) => b.text() === "Open review")
			.trigger("click");
		expect(push).toHaveBeenCalledWith("/approvals/AR1");
	});

	it("Open review is hidden when the caller cannot review", async () => {
		const w = await mountPage(
			detail({
				row: {
					...detail().row,
					status: "Needs Review",
					draft_name: "",
					draft_doctype: "",
					approval_request: "AR1",
				},
				can_review: false,
				draft: null,
			})
		);
		expect(labels(w)).not.toContain("Open review");
	});

	it("Running is one calm line", async () => {
		const w = await mountPage(
			detail({
				row: { ...detail().row, status: "Running", draft_name: "", draft_doctype: "" },
				draft: null,
			})
		);
		expect(w.find("[data-testid=intake-state]").text()).toBe("Reading now.");
	});

	it("stacks on a phone: one column that becomes two at lg, photo not pinned below lg", async () => {
		const w = await mountPage(detail());
		expect(w.find("[data-testid=intake-detail]").classes()).toContain(
			"lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]"
		);
		const photo = w.find("[data-testid=intake-photo]").classes();
		expect(photo).toContain("lg:sticky");
		expect(photo).not.toContain("sticky");
		expect(w.find("[data-testid=intake-photo] img").classes()).toContain("max-h-[40vh]");
	});

	it("shows the error state when the row cannot be opened", async () => {
		intakeDetail.mockRejectedValue(new Error("Not permitted"));
		const w = mount(IntakeDetail);
		await flushPromises();
		expect(w.text()).toContain("File not found");
	});
});
