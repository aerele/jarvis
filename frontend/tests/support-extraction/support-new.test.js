import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	FormControl: {
		props: ["modelValue"],
		template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">`,
	},
	toast: { success: vi.fn(), error: vi.fn() },
}));

const storeDouble = {
	createTicket: vi.fn(async () => "TKT-9"),
	uploadTo: vi.fn(async () => 1),
	loadTickets: vi.fn(),
};
vi.mock("@/stores/support", () => ({ useSupportStore: () => storeDouble }));

const push = vi.fn();
const replace = vi.fn();
let query = {};
vi.mock("vue-router", () => ({
	useRouter: () => ({ push, replace }),
	useRoute: () => ({ query }),
}));

import SupportNewPage from "@/pages/support/SupportNewPage.vue";

const opts = { global: { stubs: { SupportShell: { template: "<div><slot/></div>" } } } };

describe("SupportNewPage", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		query = {};
		// previewFor() calls URL.createObjectURL for image/* files; jsdom has no
		// such method (Constraint 16), so an unstubbed image emit crashes. Assign
		// onto URL rather than replacing it — class statics are non-enumerable,
		// so `{ ...URL }` spreads to `{}` and would destroy the constructor.
		URL.createObjectURL = () => "blob:x";
		URL.revokeObjectURL = () => {};
	});

	it("keeps Send disarmed until Subject has text", async () => {
		// Subject is the ticket's identity in both the list and Helpdesk — a
		// subject-less ticket is unfindable, so this is a real gate, not polish.
		const w = mount(SupportNewPage, opts);
		expect(w.findComponent({ name: "Composer" }).props("canSend")).toBe(false);
		await w.find("input").setValue("Invoice total is wrong");
		expect(w.findComponent({ name: "Composer" }).props("canSend")).toBe(true);
	});

	it("does not arm on body text alone", async () => {
		const w = mount(SupportNewPage, opts);
		w.findComponent({ name: "Composer" }).vm.$emit("update:modelValue", "lots of detail");
		await w.vm.$nextTick();
		expect(w.findComponent({ name: "Composer" }).props("canSend")).toBe(false);
	});

	it("creates, THEN uploads, THEN navigates to the new ticket", async () => {
		// supportUpload requires an existing ticket name, so create must resolve
		// first; navigating before the upload would strand the files.
		const order = [];
		storeDouble.createTicket.mockImplementation(async () => {
			order.push("create");
			return "TKT-9";
		});
		storeDouble.uploadTo.mockImplementation(async () => {
			order.push("upload");
			return 1;
		});
		const w = mount(SupportNewPage, opts);
		await w.find("input").setValue("Broken invoice");
		w.findComponent({ name: "Composer" }).vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		w.findComponent({ name: "Composer" }).vm.$emit("submit");
		await flushPromises();

		expect(order).toEqual(["create", "upload"]);
		expect(replace).toHaveBeenCalledWith({
			name: "SupportTicket",
			params: { ticket: "TKT-9" },
		});
	});

	it("keeps the draft when creation fails, instead of silently discarding it", async () => {
		storeDouble.createTicket.mockResolvedValue(null); // the store already toasted
		const w = mount(SupportNewPage, opts);
		await w.find("input").setValue("Broken invoice");
		w.findComponent({ name: "Composer" }).vm.$emit("submit");
		await flushPromises();
		expect(replace).not.toHaveBeenCalled();
		expect(w.find("input").element.value).toBe("Broken invoice");
	});

	it("prefills an editable body from the chat hook", async () => {
		query = { body: '\n\n— From Jarvis chat: "Invoice run"' };
		const w = mount(SupportNewPage, opts);
		expect(w.findComponent({ name: "Composer" }).props("modelValue")).toContain("Invoice run");
	});
});
