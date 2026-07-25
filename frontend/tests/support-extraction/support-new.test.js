import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// The New page is now a frappe-ui form (Helpdesk-style): a Subject FormControl +
// a TextEditor description + a Submit button, not the chat Composer. Stub the
// frappe-ui pieces (TextEditor is TipTap — too heavy for jsdom) and the editor
// CSS + DOMPurify side imports.
vi.mock("frappe-ui/editor-style.css", () => ({}));
vi.mock("dompurify", () => ({ default: { sanitize: (s) => s } }));
vi.mock("frappe-ui", () => ({
	FormControl: {
		props: ["modelValue"],
		template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">`,
	},
	TextEditor: { name: "TextEditor", props: ["content"], template: `<div class="editor" />` },
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: { props: ["name"], template: "<i />" },
	toast: { success: vi.fn(), error: vi.fn() },
}));

const storeDouble = {
	createTicket: vi.fn(async () => "TKT-9"),
	// uploadTo returns the succeeded FILE REFERENCES, not a count.
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

const subjectInput = (w) => w.find('input:not([type="file"])');
const editor = (w) => w.findComponent({ name: "TextEditor" });
const submitBtn = (w) => w.findComponent({ name: "Button" });
async function setDescription(w, html) {
	editor(w).vm.$emit("change", html);
	await w.vm.$nextTick();
}
async function addFiles(w, files) {
	const fi = w.find('input[type="file"]');
	Object.defineProperty(fi.element, "files", { value: files, configurable: true });
	await fi.trigger("change");
}

describe("SupportNewPage", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		query = {};
		// previewFor() calls URL.createObjectURL for image/* files; jsdom has no such
		// method. Assign onto URL rather than replacing it (class statics are
		// non-enumerable, so `{ ...URL }` spreads to `{}` and destroys the ctor).
		URL.createObjectURL = () => "blob:x";
		URL.revokeObjectURL = () => {};
	});

	it("keeps Submit disabled until BOTH the subject and a description are present", async () => {
		// Matches Helpdesk's new-ticket form: a subject alone (or a description
		// alone) is not enough to submit.
		const w = mount(SupportNewPage, opts);
		expect(submitBtn(w).props("disabled")).toBe(true);

		await subjectInput(w).setValue("Invoice total is wrong");
		expect(submitBtn(w).props("disabled")).toBe(true); // subject only

		await setDescription(w, "<p>lots of detail</p>");
		expect(submitBtn(w).props("disabled")).toBe(false);
	});

	it("does not enable Submit on description alone", async () => {
		const w = mount(SupportNewPage, opts);
		await setDescription(w, "<p>lots of detail</p>");
		expect(submitBtn(w).props("disabled")).toBe(true);
	});

	it("treats an empty TipTap doc (<p></p>) as no description", async () => {
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p></p>");
		expect(submitBtn(w).props("disabled")).toBe(true);
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
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p>details</p>");
		await addFiles(w, [{ name: "a.png", type: "image/png" }]);
		await submitBtn(w).trigger("click");
		await flushPromises();

		expect(order).toEqual(["create", "upload"]);
		expect(replace).toHaveBeenCalledWith({
			name: "SupportTicket",
			params: { ticket: "TKT-9" },
		});
	});

	it("sends the sanitized editor HTML as the body", async () => {
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p>a <strong>rich</strong> description</p>");
		await submitBtn(w).trigger("click");
		await flushPromises();
		expect(storeDouble.createTicket).toHaveBeenCalledWith(
			"Broken invoice",
			"<p>a <strong>rich</strong> description</p>"
		);
	});

	it("keeps the draft when creation fails, instead of silently discarding it", async () => {
		storeDouble.createTicket.mockResolvedValue(null); // the store already toasted
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p>details</p>");
		await submitBtn(w).trigger("click");
		await flushPromises();
		expect(replace).not.toHaveBeenCalled();
		expect(subjectInput(w).element.value).toBe("Broken invoice");
	});

	it("prefills an editable body from the chat hook", async () => {
		query = { body: '\n\n— From Jarvis chat: "Invoice run"' };
		const w = mount(SupportNewPage, opts);
		expect(editor(w).props("content")).toContain("Invoice run");
	});

	it("prefills the subject from the chat hook", async () => {
		query = { subject: "Invoice total is wrong" };
		const w = mount(SupportNewPage, opts);
		expect(subjectInput(w).element.value).toBe("Invoice total is wrong");
	});

	it("takes the first value when a query param is repeated (vue-router hands back an array)", () => {
		query = {
			subject: ["First subject", "Second subject"],
			body: ["First body", "Second body"],
		};
		const w = mount(SupportNewPage, opts);
		expect(subjectInput(w).element.value).toBe("First subject");
		expect(editor(w).props("content")).toContain("First body");
	});

	it("keeps staged files pending when uploadTo reports fewer successes than requested", async () => {
		// uploadTo returns the succeeded FILE REFERENCES; a silent clear-all would
		// discard attachments the user still needs to retry.
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		storeDouble.uploadTo = vi.fn(async () => []); // nothing succeeded
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p>details</p>");
		await addFiles(w, [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await submitBtn(w).trigger("click");
		await flushPromises();

		expect(w.findAll(".jv-supn-chip")).toHaveLength(2);
	});
});
