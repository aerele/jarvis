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
	toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
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

import { toast } from "frappe-ui";
import SupportNewPage from "@/pages/support/SupportNewPage.vue";

const opts = { global: { stubs: { SupportShell: { template: "<div><slot/></div>" } } } };

const subjectInput = (w) => w.find('input:not([type="file"])');
const editor = (w) => w.findComponent({ name: "TextEditor" });
const submitBtn = (w) => w.findComponent({ name: "Button" });
async function setDescription(w, html) {
	editor(w).vm.$emit("change", html);
	await w.vm.$nextTick();
}
// A real <input type=file>'s `.files` is a FileList — array-LIKE but NOT an
// Array. The plain-array staging the old helper used is exactly why the
// FileList-concat bug (2 files -> 1 nameless chip) slipped through: `[].concat`
// spreads an Array but appends a FileList as ONE element. Reproduce the real
// shape — indexed + length + iterable (the last so the fixed `[...files]` works;
// a bare {0,1,length} object would crash the fixed code).
function fileList(arr) {
	const fl = {
		length: arr.length,
		item: (i) => arr[i] ?? null,
		[Symbol.iterator]: function* () {
			yield* arr;
		},
	};
	arr.forEach((f, i) => (fl[i] = f));
	return fl;
}
async function addFiles(w, files) {
	const fi = w.find('input[type="file"]');
	Object.defineProperty(fi.element, "files", { value: fileList(files), configurable: true });
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

	it("stages every selected file by name, and removes exactly the chip whose X is clicked", async () => {
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		storeDouble.uploadTo = vi.fn(async (n, f) => f);
		const w = mount(SupportNewPage, opts);
		await addFiles(w, [
			{ name: "first.png", type: "image/png" },
			{ name: "second.png", type: "image/png" },
			{ name: "third.png", type: "image/png" },
		]);
		// bug (a): all three stage, each with its real filename — a FileList that
		// concat()'d as one element would have produced a single nameless chip.
		expect(w.findAll(".jv-supn-chip").map((c) => c.text())).toEqual([
			"first.png",
			"second.png",
			"third.png",
		]);

		// bug (b): removing the MIDDLE chip must drop exactly that file, not a
		// wrong index — removeFile was being handed the key string (a no-op).
		await w.findAll(".jv-supn-chip")[1].find("button").trigger("click");
		expect(w.findAll(".jv-supn-chip").map((c) => c.text())).toEqual([
			"first.png",
			"third.png",
		]);

		// and it's the underlying files that changed, not just the chips: uploadTo
		// receives exactly the two survivors, in order.
		await subjectInput(w).setValue("Broken invoice");
		await setDescription(w, "<p>details</p>");
		await submitBtn(w).trigger("click");
		await flushPromises();
		expect(storeDouble.uploadTo.mock.calls[0][1].map((f) => f.name)).toEqual([
			"first.png",
			"third.png",
		]);
	});

	it("routes pasted/dropped files into attachments and leaves plain text alone", async () => {
		const w = mount(SupportNewPage, opts);
		const card = w.find(".rounded-lg");

		await card.trigger("paste", {
			clipboardData: { files: fileList([{ name: "shot.png", type: "image/png" }]) },
		});
		expect(w.findAll(".jv-supn-chip").map((c) => c.text())).toEqual(["shot.png"]);
		expect(toast.info).toHaveBeenCalledTimes(1);

		// a files-less paste (plain text) must pass through untouched
		toast.info.mockClear();
		await card.trigger("paste", { clipboardData: { files: fileList([]) } });
		expect(w.findAll(".jv-supn-chip")).toHaveLength(1);
		expect(toast.info).not.toHaveBeenCalled();

		// drop takes the same route
		await card.trigger("drop", {
			dataTransfer: { files: fileList([{ name: "log.txt", type: "text/plain" }]) },
		});
		expect(w.findAll(".jv-supn-chip").map((c) => c.text())).toEqual(["shot.png", "log.txt"]);
	});

	it("strips inline data:/blob: images from the body before sending (they can't render server-side)", async () => {
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("Screenshot issue");
		await setDescription(w, '<p>see <img src="data:image/png;base64,AAAA"> here</p>');
		await submitBtn(w).trigger("click");
		await flushPromises();
		const sentBody = storeDouble.createTicket.mock.calls[0][1];
		expect(sentBody).not.toContain("data:image");
		expect(sentBody).toContain("see");
		expect(toast.info).toHaveBeenCalled(); // told the user they were removed
	});

	it("rejects an oversize attachment with a message instead of staging it", async () => {
		const w = mount(SupportNewPage, opts);
		await addFiles(w, [
			{ name: "huge.zip", size: 26 * 1024 * 1024 },
			{ name: "ok.png", type: "image/png", size: 1000 },
		]);
		expect(w.findAll(".jv-supn-chip").map((c) => c.text())).toEqual(["ok.png"]);
		expect(toast.error).toHaveBeenCalled();
	});

	it("caps the subject at 140 chars (a ?subject= prefill bypasses the input maxlength)", async () => {
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("x".repeat(200));
		await setDescription(w, "<p>d</p>");
		await submitBtn(w).trigger("click");
		await flushPromises();
		expect(storeDouble.createTicket.mock.calls[0][0]).toHaveLength(140);
	});

	it("treats a whitespace-only (&nbsp;) description as empty", async () => {
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("S");
		await setDescription(w, "<p>&nbsp;</p>");
		expect(submitBtn(w).props("disabled")).toBe(true);
	});

	it("does not upload a chip removed while the submit was in flight (no un-attach on Helpdesk)", async () => {
		let resolveCreate;
		storeDouble.createTicket = vi.fn(() => new Promise((r) => (resolveCreate = r)));
		storeDouble.uploadTo = vi.fn(async (n, f) => f);
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("S");
		await setDescription(w, "<p>d</p>");
		await addFiles(w, [
			{ name: "keep.png", type: "image/png" },
			{ name: "drop.png", type: "image/png" },
		]);
		submitBtn(w).trigger("click"); // create() now awaits createTicket
		await flushPromises();
		await w.findAll(".jv-supn-chip")[1].find("button").trigger("click"); // remove drop.png
		resolveCreate("TKT-9");
		await flushPromises();
		expect(storeDouble.uploadTo.mock.calls[0][1].map((f) => f.name)).toEqual(["keep.png"]);
	});

	it("does not replace the router if unmounted during the post-create list refresh (I5 tail)", async () => {
		storeDouble.createTicket = vi.fn(async () => "TKT-9");
		storeDouble.uploadTo = vi.fn(async (n, f) => f);
		let resolveLoad;
		storeDouble.loadTickets = vi.fn(() => new Promise((r) => (resolveLoad = r)));
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("S");
		await setDescription(w, "<p>d</p>");
		submitBtn(w).trigger("click");
		await flushPromises(); // create + upload done, now awaiting loadTickets
		w.unmount();
		resolveLoad();
		await flushPromises();
		expect(replace).not.toHaveBeenCalled();
	});

	it("warns about files attached after submit began (they'd be lost on navigate) (I6)", async () => {
		let resolveCreate;
		storeDouble.createTicket = vi.fn(() => new Promise((r) => (resolveCreate = r)));
		storeDouble.uploadTo = vi.fn(async (n, f) => f);
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("S");
		await setDescription(w, "<p>d</p>");
		submitBtn(w).trigger("click"); // awaiting createTicket, nothing staged yet
		await flushPromises();
		await addFiles(w, [{ name: "late.png", type: "image/png" }]); // attached mid-submit
		resolveCreate("TKT-9");
		await flushPromises();
		expect(storeDouble.uploadTo).not.toHaveBeenCalled(); // nothing was staged at submit start
		expect(toast.info).toHaveBeenCalled(); // user told the late file wasn't attached
	});

	it("normalizes CRLF in the chat-hook body prefill (M4)", () => {
		query = { body: "line one\r\n\r\nline two" };
		const w = mount(SupportNewPage, opts);
		expect(editor(w).props("content")).toBe("<p>line one</p><p>line two</p>");
	});

	it("does not hijack the router if the user navigated away mid-submit", async () => {
		let resolveCreate;
		storeDouble.createTicket = vi.fn(() => new Promise((r) => (resolveCreate = r)));
		storeDouble.uploadTo = vi.fn(async (n, f) => f);
		const w = mount(SupportNewPage, opts);
		await subjectInput(w).setValue("S");
		await setDescription(w, "<p>d</p>");
		submitBtn(w).trigger("click");
		await flushPromises();
		w.unmount(); // user pressed Back while create was in flight
		resolveCreate("TKT-9");
		await flushPromises();
		expect(replace).not.toHaveBeenCalled();
	});

	it("preventDefaults BEFORE staging/toast, so a later throw can't reopen the inline path", async () => {
		// The capture handler must contain the event first; if stage/toast ran
		// first and threw, the prevent would be skipped and ProseMirror would take
		// the paste inline. Assert the ordering via the global invocation counter.
		const w = mount(SupportNewPage, opts);
		toast.info.mockClear();
		const prevent = vi.spyOn(Event.prototype, "preventDefault");
		await w.find(".rounded-lg").trigger("paste", {
			clipboardData: { files: fileList([{ name: "x.png", type: "image/png" }]) },
		});
		expect(prevent).toHaveBeenCalled();
		expect(prevent.mock.invocationCallOrder[0]).toBeLessThan(
			toast.info.mock.invocationCallOrder[0]
		);
		prevent.mockRestore();
	});
});
