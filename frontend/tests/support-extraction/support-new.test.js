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
	// Fix 2: uploadTo returns the succeeded FILE REFERENCES, not a count — a
	// realistic default double is "everything I was given succeeded".
	uploadTo: vi.fn(async (name, files) => files),
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
		storeDouble.uploadTo.mockImplementation(async (name, files) => {
			order.push("upload");
			return files;
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

	it("prefills the subject from the chat hook", async () => {
		query = { subject: "Invoice total is wrong" };
		const w = mount(SupportNewPage, opts);
		expect(w.find("input").element.value).toBe("Invoice total is wrong");
	});

	it("takes the first value when a query param is repeated (vue-router hands back an array)", () => {
		// Minor: ?subject=a&subject=b makes vue-router's route.query.subject an
		// ARRAY, not a string — String([...]) would silently join it with a
		// comma instead of taking the first value.
		query = {
			subject: ["First subject", "Second subject"],
			body: ["First body", "Second body"],
		};
		const w = mount(SupportNewPage, opts);
		expect(w.find("input").element.value).toBe("First subject");
		expect(w.findComponent({ name: "Composer" }).props("modelValue")).toBe("First body");
	});

	it("keeps staged files pending when uploadTo reports fewer successes than requested", async () => {
		// Proof of fix 2: uploadTo returns the succeeded FILE REFERENCES. A
		// silent `files.value = []` here would discard attachments the user
		// still needs to retry after a transient upload failure, even though the
		// ticket itself was created successfully.
		// Reassign both fresh: earlier tests in this file mutate createTicket's
		// mock via .mockImplementation/.mockResolvedValue, which — unlike
		// vi.clearAllMocks() in beforeEach — outlives the test that set it. A
		// stale `createTicket` resolving to null would short-circuit before
		// uploadTo is ever reached and pass this test for the wrong reason.
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		storeDouble.uploadTo = vi.fn(async () => []); // nothing succeeded
		const w = mount(SupportNewPage, opts);
		await w.find("input").setValue("Broken invoice");
		w.findComponent({ name: "Composer" }).vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		w.findComponent({ name: "Composer" }).vm.$emit("submit");
		await flushPromises();

		expect(w.findComponent({ name: "Composer" }).props("attachments")).toHaveLength(2);
	});
});
