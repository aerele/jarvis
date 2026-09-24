import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		props: ["options", "placeholder"],
		emits: ["update:modelValue", "update:query"],
		template: '<input class="ac" :placeholder="placeholder" />',
	},
	Button: {
		props: ["label", "disabled", "loading"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :aria-label="$attrs[\'aria-label\']" @click="$emit(\'click\')">{{ label }}</button>',
	},
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api/approvals", () => ({
	getPendingAction: vi.fn(),
	decideHeldAction: vi.fn(),
	editAndCreateHeld: vi.fn(),
}));
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []), getDoctypeFormMeta: vi.fn() }));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { props: ["label"], template: '<span role="status">{{ label }}</span>' },
}));

import { toast } from "frappe-ui";
import * as api from "@/api/approvals";
import * as core from "@/api";
import PendingActionDetail from "./PendingActionDetail.vue";

const HOSTILE = '<img src=x onerror="window.__pwned=1">';

const rec = (over = {}) => ({
	name: "PA-1",
	kind: "file_box_held",
	status: "Pending",
	summary: "New supplier: Acme",
	card: {
		kind: "create",
		doctype: "Supplier",
		name: null,
		rows: [{ label: "Supplier Name", value: "Acme" }],
		tables: [],
	},
	doctype: "Supplier",
	waiters_count: 2,
	candidates: [],
	can_act: 1,
	can_use_existing: 1,
	reason_code: "",
	for_user: "",
	...over,
});

let onDecided;
const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

async function mountWith(r) {
	api.getPendingAction.mockResolvedValue(r);
	const w = mount(PendingActionDetail, {
		props: { name: "PA-1", onDecided },
		attachTo: document.body,
	});
	await flushPromises();
	return w;
}

describe("PendingActionDetail", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		onDecided = vi.fn();
	});

	it("shows loading, then the card and the three decisions", async () => {
		let resolve;
		api.getPendingAction.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mount(PendingActionDetail, { props: { name: "PA-1", onDecided } });
		expect(w.text()).toContain("Loading the proposed record");
		resolve(rec());
		await flushPromises();
		expect(w.text()).toContain("2 files waiting");
		expect(w.text()).toContain("Acme");
		expect(button(w, "Create & continue")).toBeTruthy();
		expect(button(w, "Use existing")).toBeTruthy();
		expect(button(w, "Don't create — skip 2 files")).toBeTruthy();
	});

	it("shows a load error with a retry", async () => {
		api.getPendingAction.mockRejectedValueOnce(
			new Error("This approval is no longer available.")
		);
		const w = mount(PendingActionDetail, { props: { name: "PA-1", onDecided } });
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("no longer available");
		api.getPendingAction.mockResolvedValueOnce(rec());
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(button(w, "Create & continue")).toBeTruthy();
	});

	it("renders hostile model text as text, never HTML", async () => {
		const w = await mountWith(
			rec({
				summary: HOSTILE,
				card: {
					kind: "create",
					doctype: "Supplier",
					rows: [{ label: HOSTILE, value: HOSTILE }],
					tables: [],
				},
				candidates: [{ name: HOSTILE, title: HOSTILE, gstin: "" }],
			})
		);
		await button(w, "Use existing").trigger("click");
		// Attribute values (the aria-label) may carry the text; no element is ever built.
		expect(w.find("img").exists()).toBe(false);
		expect(w.element.querySelectorAll("img, script").length).toBe(0);
		expect(w.text()).toContain(HOSTILE);
		expect(window.__pwned).toBeUndefined();
	});

	it("creates, toasts the outcome and emits decided", async () => {
		const w = await mountWith(rec());
		api.decideHeldAction.mockResolvedValue({
			ok: true,
			reason_code: "created",
			waiters_count: 2,
		});
		await button(w, "Create & continue").trigger("click");
		await flushPromises();
		expect(api.decideHeldAction).toHaveBeenCalledWith("PA-1", "create", undefined);
		expect(toast.success).toHaveBeenCalledWith("Created. The 2 waiting files continue.");
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("is busy while deciding: a second click does nothing", async () => {
		const w = await mountWith(rec());
		let resolve;
		api.decideHeldAction.mockReturnValue(new Promise((r) => (resolve = r)));
		await button(w, "Create & continue").trigger("click");
		expect(button(w, "Don't create — skip 2 files").attributes("disabled")).toBeDefined();
		await button(w, "Don't create — skip 2 files").trigger("click");
		resolve({ ok: true, reason_code: "created", waiters_count: 2 });
		await flushPromises();
		expect(api.decideHeldAction).toHaveBeenCalledTimes(1);
	});

	it("an existing party hands the approver to Use existing, focused", async () => {
		const w = await mountWith(rec());
		api.decideHeldAction.mockResolvedValue({
			ok: false,
			reason_code: "exists",
			error: { message: "Supplier Acme already exists now. Choose Use existing." },
		});
		api.getPendingAction.mockResolvedValue(
			rec({ candidates: [{ name: "SUP-1", title: "Acme", gstin: "" }] })
		);
		await button(w, "Create & continue").trigger("click");
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("already exists now");
		expect(onDecided).not.toHaveBeenCalled();
		const panel = w.find("#held-existing-PA-1");
		expect(panel.exists()).toBe(true);
		expect(document.activeElement).toBe(panel.element);
		api.decideHeldAction.mockResolvedValue({
			ok: true,
			reason_code: "use_existing",
			waiters_count: 2,
			data: { name: "SUP-1" },
		});
		await w.find('button[aria-label="Use Acme"]').trigger("click");
		await flushPromises();
		expect(api.decideHeldAction).toHaveBeenLastCalledWith("PA-1", "use_existing", "SUP-1");
		expect(toast.success).toHaveBeenCalledWith("Using SUP-1. The 2 waiting files continue.");
		w.unmount();
	});

	it("skips, and a terminal failure still leaves the lane", async () => {
		const w = await mountWith(rec());
		api.decideHeldAction.mockResolvedValue({
			ok: false,
			reason_code: "failed",
			pa_status: "Failed",
			error: { message: "Supplier Group is required" },
		});
		await button(w, "Create & continue").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"Couldn&#39;t create: Supplier Group is required"
		);
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("keeps the row on a transient refusal and shows why", async () => {
		const w = await mountWith(rec());
		api.decideHeldAction.mockResolvedValue({ ok: false, reason_code: "busy" });
		await button(w, "Don't create — skip 2 files").trigger("click");
		await flushPromises();
		expect(api.decideHeldAction).toHaveBeenCalledWith("PA-1", "skip", undefined);
		expect(w.find('[role="alert"]').text()).toContain("Try again in a moment");
		expect(onDecided).not.toHaveBeenCalled();
	});

	it("a decided row is read-only", async () => {
		const w = await mountWith(rec({ status: "Discarded", can_act: 0 }));
		expect(button(w, "Create & continue")).toBeFalsy();
		expect(w.find('[role="status"]').text()).toContain("Skipped");
	});

	it("refresh shows a row settled elsewhere, but never over a decision in flight", async () => {
		const w = await mountWith(rec());
		let resolve;
		api.decideHeldAction.mockReturnValue(new Promise((r) => (resolve = r)));
		await button(w, "Create & continue").trigger("click");
		w.vm.refresh();
		expect(api.getPendingAction).toHaveBeenCalledTimes(1);
		resolve({ ok: false, reason_code: "busy" });
		await flushPromises();
		api.getPendingAction.mockResolvedValue(rec({ status: "Executed", can_act: 0 }));
		w.vm.refresh();
		await flushPromises();
		expect(w.find('[role="status"]').text()).toBe("Created.");
		expect(button(w, "Create & continue")).toBeFalsy();
	});

	it("reports the outcome even after the board switched away mid-call", async () => {
		const w = await mountWith(rec());
		let resolve;
		api.decideHeldAction.mockReturnValue(new Promise((r) => (resolve = r)));
		await button(w, "Create & continue").trigger("click");
		w.unmount();
		resolve({ ok: true, reason_code: "created", waiters_count: 2 });
		await flushPromises();
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("a batch offers no Use existing", async () => {
		const w = await mountWith(rec({ can_use_existing: 0, doctype: "" }));
		expect(button(w, "Use existing")).toBeFalsy();
	});

	describe("missing fields + Edit & create", () => {
		const META = {
			ok: true,
			fields: [
				{ fieldname: "supplier_name", label: "Supplier Name", fieldtype: "Data", reqd: 1 },
				{
					fieldname: "supplier_type",
					label: "Supplier Type",
					fieldtype: "Select",
					options: "\nCompany\nIndividual",
					reqd: 1,
				},
				{ fieldname: "tax_id", label: "Tax ID", fieldtype: "Data" },
			],
			tables: {},
		};
		const held = (over = {}) =>
			rec({
				can_edit: 1,
				docs: [
					{
						doctype: "Supplier",
						values: {
							supplier_name: "Acme",
							tax_id: "27ABC",
							supplier_type: "",
							bank_api_key: "••••••",
						},
						secret: ["bank_api_key"],
						locked: {
							supplier_name: "It identifies this record.",
							tax_id: "It identifies this record.",
						},
					},
				],
				needs_input: [
					{
						doc_index: 0,
						doctype: "Supplier",
						fieldname: "supplier_type",
						label: "Supplier Type",
					},
				],
				...over,
			});
		const control = (w, label) => {
			const l = w.findAll("label").find((x) => x.text().replace("*", "").trim() === label);
			return w.find("#" + l.attributes("for"));
		};
		async function editing(r = held()) {
			core.getDoctypeFormMeta.mockResolvedValue(META);
			const w = await mountWith(r);
			await button(w, "Edit & create").trigger("click");
			await flushPromises();
			return w;
		}

		it("Create & continue is disabled while fields are missing, with the reason", async () => {
			const w = await mountWith(held());
			expect(w.text()).toContain("Missing: Supplier Type");
			const create = button(w, "Create & continue");
			expect(create.attributes("disabled")).toBeDefined();
			const reason = w.find("#" + create.attributes("aria-describedby"));
			expect(reason.text()).toBe("Fill Supplier Type — use Edit & create.");
			await create.trigger("click");
			expect(api.decideHeldAction).not.toHaveBeenCalled();
		});

		it("fills the missing field and creates with only the changed values", async () => {
			const w = await editing();
			expect(core.getDoctypeFormMeta).toHaveBeenCalledWith("Supplier");
			const create = button(w, "Create & continue");
			expect(create.attributes("disabled")).toBeDefined();
			expect(w.find("#" + create.attributes("aria-describedby")).text()).toBe(
				"Fill Supplier Type first."
			);
			expect(control(w, "Tax ID").element.tagName).toBe("DIV"); // locked
			await control(w, "Supplier Type").setValue("Company");
			expect(button(w, "Create & continue").attributes("disabled")).toBeUndefined();
			api.editAndCreateHeld.mockResolvedValue({
				ok: true,
				reason_code: "created",
				waiters_count: 2,
			});
			await button(w, "Create & continue").trigger("click");
			await flushPromises();
			expect(api.editAndCreateHeld).toHaveBeenCalledWith("PA-1", [
				{ supplier_type: "Company" },
			]);
			expect(toast.success).toHaveBeenCalledWith("Created. The 2 waiting files continue.");
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("shows server field errors inline and the message as a banner; Cancel goes back", async () => {
			const w = await editing();
			await control(w, "Supplier Type").setValue("Company");
			api.editAndCreateHeld.mockResolvedValue({
				ok: false,
				reason_code: "invalid",
				error: { message: "Fill the highlighted fields." },
				errors: [{ doc_index: 0, fieldname: "supplier_type", message: "Not allowed" }],
			});
			await button(w, "Create & continue").trigger("click");
			await flushPromises();
			expect(w.find('[role="alert"]').text()).toBe("Fill the highlighted fields.");
			const type = control(w, "Supplier Type");
			expect(w.find("#" + type.attributes("aria-describedby")).text()).toBe("Not allowed");
			expect(onDecided).not.toHaveBeenCalled();
			await button(w, "Cancel").trigger("click");
			expect(button(w, "Edit & create")).toBeTruthy();
		});

		it("an existing party found on submit hands the approver to Use existing, focused", async () => {
			const w = await editing();
			await control(w, "Supplier Type").setValue("Company");
			api.editAndCreateHeld.mockResolvedValue({
				ok: false,
				reason_code: "exists",
				error: { message: "Supplier Acme already exists now. Choose Use existing." },
			});
			api.getPendingAction.mockResolvedValue(
				held({ candidates: [{ name: "SUP-1", title: "Acme", gstin: "" }] })
			);
			await button(w, "Create & continue").trigger("click");
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
			expect(button(w, "Edit & create")).toBeTruthy(); // out of edit mode
			expect(w.find('[role="alert"]').text()).toContain("already exists now");
			expect(onDecided).not.toHaveBeenCalled();
			const panel = w.find("#held-existing-PA-1");
			expect(panel.exists()).toBe(true);
			expect(document.activeElement).toBe(panel.element);
			expect(w.find('button[aria-label="Use Acme"]').exists()).toBe(true);
			w.unmount();
		});

		it("a failure after the claim keeps the values on screen until Close", async () => {
			const w = await editing();
			await control(w, "Supplier Type").setValue("Company");
			api.editAndCreateHeld.mockResolvedValue({
				ok: false,
				reason_code: "failed",
				pa_status: "Failed",
				error: { message: "Supplier Type cannot be Bogus" },
			});
			await button(w, "Create & continue").trigger("click");
			await flushPromises();
			expect(w.find('[role="alert"]').text()).toBe(
				"Couldn't create: Supplier Type cannot be Bogus"
			);
			expect(control(w, "Supplier Type").element.value).toBe("Company");
			expect(button(w, "Create & continue").attributes("disabled")).toBeDefined();
			expect(onDecided).not.toHaveBeenCalled();
			await button(w, "Close").trigger("click");
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("refresh never wipes an open edit or a failure's values", async () => {
			const w = await editing();
			await control(w, "Supplier Type").setValue("Company");
			w.vm.refresh();
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(1);
			expect(control(w, "Supplier Type").element.value).toBe("Company");
		});

		it("refresh leaves a failure's values and its Close on screen", async () => {
			const w = await editing();
			await control(w, "Supplier Type").setValue("Company");
			api.editAndCreateHeld.mockResolvedValue({
				ok: false,
				reason_code: "failed",
				pa_status: "Failed",
				error: { message: "Supplier Type cannot be Bogus" },
			});
			await button(w, "Create & continue").trigger("click");
			await flushPromises();
			api.getPendingAction.mockResolvedValue(held({ status: "Failed", can_act: 0 }));
			w.vm.refresh();
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(1);
			expect(control(w, "Supplier Type").element.value).toBe("Company");
			expect(w.find('[role="alert"]').text()).toContain("Supplier Type cannot be Bogus");
			await button(w, "Close").trigger("click");
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("links the full Desk form with non-secret values, in a new tab", async () => {
			const w = await editing();
			const a = w.find("a");
			expect(a.attributes("href")).toBe("/app/supplier/new?supplier_name=Acme&tax_id=27ABC");
			expect(a.attributes("target")).toBe("_blank");
			expect(a.attributes("rel")).toBe("noopener");
		});

		it("a form that can't load still offers the Desk escape hatch", async () => {
			core.getDoctypeFormMeta.mockRejectedValue(new Error("403"));
			const w = await mountWith(held());
			await button(w, "Edit & create").trigger("click");
			await flushPromises();
			expect(w.find('[role="alert"]').text()).toContain("full form in Desk");
			expect(w.find("a").exists()).toBe(true);
		});

		it("renders hostile proposed values as text", async () => {
			const r = held();
			r.docs[0].values.supplier_name = HOSTILE;
			r.needs_input[0].label = HOSTILE;
			const w = await editing(r);
			expect(w.element.querySelectorAll("img, script").length).toBe(0);
			expect(w.text()).toContain(HOSTILE);
			expect(window.__pwned).toBeUndefined();
		});
	});
});
