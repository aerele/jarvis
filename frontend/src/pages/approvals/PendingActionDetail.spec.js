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
vi.mock("@/api/approvals", () => ({ getPendingAction: vi.fn(), decideHeldAction: vi.fn() }));
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []) }));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { props: ["label"], template: '<span role="status">{{ label }}</span>' },
}));

import { toast } from "frappe-ui";
import * as api from "@/api/approvals";
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

const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

async function mountWith(r) {
	api.getPendingAction.mockResolvedValue(r);
	const w = mount(PendingActionDetail, { props: { name: "PA-1" }, attachTo: document.body });
	await flushPromises();
	return w;
}

describe("PendingActionDetail", () => {
	beforeEach(() => vi.clearAllMocks());

	it("shows loading, then the card and the three decisions", async () => {
		let resolve;
		api.getPendingAction.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mount(PendingActionDetail, { props: { name: "PA-1" } });
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
		const w = mount(PendingActionDetail, { props: { name: "PA-1" } });
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
		expect(w.emitted("decided")).toHaveLength(1);
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
		expect(w.emitted("decided")).toBeUndefined();
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
		expect(w.emitted("decided")).toHaveLength(1);
	});

	it("keeps the row on a transient refusal and shows why", async () => {
		const w = await mountWith(rec());
		api.decideHeldAction.mockResolvedValue({ ok: false, reason_code: "busy" });
		await button(w, "Don't create — skip 2 files").trigger("click");
		await flushPromises();
		expect(api.decideHeldAction).toHaveBeenCalledWith("PA-1", "skip", undefined);
		expect(w.find('[role="alert"]').text()).toContain("Try again in a moment");
		expect(w.emitted("decided")).toBeUndefined();
	});

	it("a decided row is read-only", async () => {
		const w = await mountWith(rec({ status: "Discarded", can_act: 0 }));
		expect(button(w, "Create & continue")).toBeFalsy();
		expect(w.find('[role="status"]').text()).toContain("Skipped");
	});

	it("a batch offers no Use existing", async () => {
		const w = await mountWith(rec({ can_use_existing: 0, doctype: "" }));
		expect(button(w, "Use existing")).toBeFalsy();
	});
});
