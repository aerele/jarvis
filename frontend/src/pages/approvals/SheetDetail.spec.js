import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));
vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "placeholder", "modelValue", "loading"],
		emits: ["update:modelValue", "update:query"],
		template: '<input class="ac" :placeholder="placeholder" />',
	},
	Badge: { props: ["label"], template: '<span class="badge">{{ label }}</span>' },
	Button: {
		props: ["label", "disabled", "loading", "variant", "theme", "size", "iconLeft"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :data-variant="variant" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	FormControl: {
		props: ["label", "placeholder", "modelValue", "type"],
		emits: ["update:modelValue"],
		template:
			'<label class="fc">{{ label }}<textarea :placeholder="placeholder" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /></label>',
	},
	confirmDialog: vi.fn(),
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api/approvals", () => ({
	getPendingAction: vi.fn(),
	getSheetStatus: vi.fn(),
	getSheetCandidates: vi.fn(),
	applySheet: vi.fn(),
	discardSheet: vi.fn(),
}));
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []), getDoctypeFormMeta: vi.fn() }));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { props: ["label"], template: '<span role="status">{{ label }}</span>' },
}));

import { Autocomplete, confirmDialog, toast } from "frappe-ui";
import * as api from "@/api/approvals";
import * as core from "@/api";
import SheetDetail from "./SheetDetail.vue";

const HOSTILE = '<img src=x onerror="window.__pwned=1">';
const HSN_FIX = "HSN/SAC Code is required. Please enter a valid HSN/SAC code.";
// the server names the field; the message need not name its label
const HSN_FLAG = { field: "gst_hsn_code", label: "HSN/SAC", message: "Enter a valid code." };
const HSN = { fieldname: "gst_hsn_code", label: "HSN/SAC", fieldtype: "Data" };
const ITEM_META = {
	ok: true,
	fields: [
		{ fieldname: "item_code", label: "Item Code", fieldtype: "Data" },
		{ fieldname: "item_name", label: "Item Name", fieldtype: "Data" },
		HSN,
	],
	tables: {},
};

const record = (index, over = {}) => ({
	index,
	doctype: "Item",
	op: "create",
	name: "",
	title: "Item " + index,
	category: "item",
	depends_on: [],
	values: { item_code: "I-" + index, item_name: "Item " + index },
	secret: [],
	locked: {},
	needs_input: [],
	needs_fix: null,
	...over,
});
const missingHsn = (index) => ({
	doc_index: index,
	doctype: "Item",
	fieldname: "gst_hsn_code",
	label: "HSN/SAC",
	field: HSN,
});

function records() {
	return [
		record(0, {
			doctype: "Supplier",
			category: "party",
			title: "Acme",
			values: { supplier_name: "Acme", tax_id: "27AAACA" },
			locked: { tax_id: "It identifies this record." },
		}),
		record(1, {
			doctype: "Address",
			category: "address",
			title: "Acme-Billing",
			depends_on: [0],
		}),
		record(2, { doctype: "Contact", category: "address", title: "Asha", depends_on: [1] }),
		record(3),
		record(4),
		record(5, { op: "update", name: "ITEM-5", title: "ITEM-5" }),
		record(6, { doctype: "UOM", category: "other", title: "Box" }),
	];
}
const questions = () => [
	{
		name: "AR-1",
		title: "Freight",
		question: "Book the freight line?",
		options: ["Yes", "No"],
		context_md: "**Freight** is ₹500",
		routing: "",
		status: "Pending",
	},
	{
		name: "AR-2",
		title: "Skill",
		question: "Which skill reads this file?",
		options: ["Purchase invoice", "Skip this file"],
		context_md: "",
		routing: "skill_conflict",
		status: "Pending",
	},
];
const detail = (over = {}) => ({
	name: "PA-S1",
	kind: "file_box_sheet",
	status: "Pending",
	summary: "Approval sheet: loreal.pdf",
	file_name: "loreal.pdf",
	conversation: "conv-1",
	for_user: "",
	collecting: 0,
	can_act: 1,
	record_count: 7,
	question_count: 2,
	counts_line: "1 supplier · 1 address · 1 contact · 3 items · 1 uom · 2 questions",
	card_sha256: "sha-1",
	records: records(),
	questions: questions(),
	errors: {},
	decisions: {},
	progress: null,
	outcome: {},
	reason_code: "",
	reason: "",
	...over,
});

function fakeSocket() {
	const handlers = new Set();
	return {
		on: (event, fn) => event === "jarvis:event" && handlers.add(fn),
		off: (event, fn) => handlers.delete(fn),
		emit: (payload) => handlers.forEach((fn) => fn(payload)),
		handlers,
	};
}

let onDecided, onChanged, socket, mounted;
async function mountSheet(d = detail(), props = {}) {
	api.getPendingAction.mockResolvedValue(d);
	const w = mount(SheetDetail, {
		props: { name: "PA-S1", onDecided, onChanged, ...props },
		attachTo: document.body,
		global: { provide: { $socket: socket } },
	});
	mounted.push(w);
	await flushPromises();
	return w;
}

const button = (w, label) => w.findAll("button").find((b) => b.text() === label);
const buttons = (w, label) => w.findAll("button").filter((b) => b.text() === label);
const row = (w, index) => w.find(`#sheet-PA-S1-r${index}`);
const choice = (w, index, label) =>
	row(w, index)
		.find('[role="group"]')
		.findAll("button")
		.find((b) => b.text() === label);
const pressed = (w, index) =>
	row(w, index)
		.find('[role="group"]')
		.findAll("button")
		.find((b) => b.attributes("aria-pressed") === "true")
		.text();
const section = (w, key) => w.find(`[aria-labelledby="sheet-PA-S1-${key}-title"]`);
const apply = (w) => button(w, "Apply & continue");
// the footer's (a routing question may offer an answer of the same name)
const skipFile = (w) => buttons(w, "Skip this file").at(-1);

async function answerAll(w) {
	await button(w, "Yes").trigger("click");
	await button(w, "Purchase invoice").trigger("click");
}
// the default fixture: records 3 and 4 need their HSN/SAC
function flagged() {
	const d = detail();
	d.records[3].needs_input = [missingHsn(3)];
	d.records[4].needs_fix = HSN_FLAG;
	return d;
}

describe("SheetDetail", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		onDecided = vi.fn();
		onChanged = vi.fn();
		socket = fakeSocket();
		mounted = [];
		core.getDoctypeFormMeta.mockResolvedValue(ITEM_META);
		api.getSheetCandidates.mockResolvedValue({});
		confirmDialog.mockImplementation(({ onConfirm }) => onConfirm({ hideDialog: vi.fn() }));
	});
	afterEach(() => {
		mounted.forEach((w) => w.unmount());
		vi.useRealTimers();
	});

	it("loads, then lists the sections in order with counts, and the header", async () => {
		let resolve;
		api.getPendingAction.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mount(SheetDetail, { props: { name: "PA-S1", onDecided } });
		mounted.push(w);
		expect(w.text()).toContain("Loading the approval sheet");
		resolve(detail({ for_user: "Asha" }));
		await flushPromises();
		const headings = w.findAll("h3").map((h) => h.findAll("span").map((s) => s.text()));
		expect(headings).toEqual([
			["Supplier", "1"],
			["Address / Contact", "2"],
			["Items", "3"],
			["Other masters", "1"],
			["Questions", "2"],
		]);
		expect(w.text()).toContain("loreal.pdf · 1 supplier · 1 address");
		expect(w.text()).toContain("for Asha");
		expect(button(w, "Open chat")).toBeFalsy(); // someone else's conversation
		expect(button(w, "Apply all")).toBeFalsy(); // Items mixes creates and an update
		expect(buttons(w, "Create all")).toHaveLength(4);
	});

	it("leaves the file name to a heading that already shows it", async () => {
		const w = await mountSheet(detail(), { showFileName: false });
		expect(w.text()).not.toContain("loreal.pdf");
		expect(w.text()).toContain("1 supplier · 1 address");
	});

	it("opens its own conversation", async () => {
		const w = await mountSheet();
		await button(w, "Open chat").trigger("click");
		expect(router.push).toHaveBeenCalledWith("/c/conv-1");
	});

	it("shows a load error with a retry", async () => {
		api.getPendingAction.mockRejectedValueOnce(
			new Error("This approval is no longer available.")
		);
		const w = mount(SheetDetail, { props: { name: "PA-S1", onDecided } });
		mounted.push(w);
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("no longer available");
		api.getPendingAction.mockResolvedValueOnce(detail());
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(apply(w)).toBeTruthy();
	});

	it("collapses a section with aria-expanded / aria-controls", async () => {
		const w = await mountSheet();
		const toggle = section(w, "item").find("h3 button");
		expect(toggle.attributes("aria-expanded")).toBe("true");
		const body = w.find("#" + toggle.attributes("aria-controls"));
		expect(body.exists()).toBe(true);
		expect(body.text()).toContain("Item 3");
		await toggle.trigger("click");
		expect(toggle.attributes("aria-expanded")).toBe("false");
		expect(w.find("#" + toggle.attributes("aria-controls")).text()).toBe("");
	});

	it("renders model text as text, never HTML", async () => {
		const d = detail({ file_name: HOSTILE, counts_line: HOSTILE });
		d.records[0].title = HOSTILE;
		d.records[0].values.tax_id = HOSTILE;
		d.records[4].needs_fix = HOSTILE;
		d.records[3].needs_fix = { field: null, label: HOSTILE, message: HOSTILE };
		d.questions[0].question = HOSTILE;
		d.questions[0].options = [HOSTILE, "No"];
		d.questions[0].context_md = HOSTILE;
		const w = await mountSheet(d);
		expect(w.element.querySelectorAll("img, script").length).toBe(0);
		expect(w.text()).toContain(HOSTILE);
		expect(window.__pwned).toBeUndefined();
	});

	it("still listing: says so, and offers no decision", async () => {
		const w = await mountSheet(detail({ collecting: 1, can_act: 0 }));
		expect(w.find('[role="status"]').text()).toContain("Still listing…");
		expect(apply(w)).toBeFalsy();
		expect(skipFile(w)).toBeFalsy();
	});

	it("PD-3: polls the slim status while listing; a full reload only once it seals", async () => {
		vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
		const w = await mountSheet(
			detail({ collecting: 1, can_act: 0, card_sha256: "sha-collect" })
		);
		api.getSheetStatus.mockResolvedValue({
			name: "PA-S1",
			status: "Pending",
			collecting: 1,
			progress: null,
			card_sha256: "sha-collect",
			record_count: 0,
			counts_line: "still counting",
		});
		await vi.advanceTimersByTimeAsync(5000);
		expect(api.getSheetStatus).toHaveBeenCalledTimes(1);
		expect(api.getPendingAction).toHaveBeenCalledTimes(1); // same status/sha: no reload
		expect(w.find('[role="status"]').text()).toContain("Still listing…");
		// the run seals: a new card_sha256 triggers a full reload
		api.getSheetStatus.mockResolvedValue({
			name: "PA-S1",
			status: "Pending",
			collecting: 0,
			progress: null,
			card_sha256: "sha-sealed",
			record_count: 7,
			counts_line: "1 supplier",
		});
		api.getPendingAction.mockResolvedValue(detail({ card_sha256: "sha-sealed" }));
		await vi.advanceTimersByTimeAsync(5000);
		expect(api.getPendingAction).toHaveBeenCalledTimes(2);
		expect(apply(w)).toBeTruthy();
	});

	describe("a pending sheet it can't verify", () => {
		const unverified = () => detail({ can_act: 0 });

		it("offers Try again, which reads it again", async () => {
			const w = await mountSheet(unverified());
			expect(w.find('[role="status"]').text()).toBe(
				"This sheet can't be verified right now, so it can't be applied. Try again, or skip this file."
			);
			expect(apply(w)).toBeFalsy();
			api.getPendingAction.mockResolvedValue(detail());
			await button(w, "Try again").trigger("click");
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
			expect(apply(w)).toBeTruthy();
		});

		it("offers Skip this file, which discards it", async () => {
			const w = await mountSheet(unverified());
			api.discardSheet.mockResolvedValue({ ok: false, reason_code: "busy" });
			await skipFile(w).trigger("click");
			await flushPromises();
			expect(api.discardSheet).toHaveBeenCalledWith("PA-S1");
			const alert = w.find('[role="alert"]');
			expect(alert.text()).toBe(
				"Someone is acting on this sheet right now. Try again in a moment."
			);
			expect(alert.classes()).toContain("text-ink-red-4"); // a frappe-ui token
			api.discardSheet.mockResolvedValue({ ok: true, reason_code: "discarded" });
			await skipFile(w).trigger("click");
			await flushPromises();
			expect(onDecided).toHaveBeenCalledTimes(1);
		});
	});

	it("a bulk choice closes a record's panel it no longer fits", async () => {
		const w = await mountSheet(flagged());
		await choice(w, 3, "Edit").trigger("click");
		await flushPromises();
		expect(w.find("#sheet-PA-S1-r3-edit").exists()).toBe(true);
		await section(w, "item")
			.findAll("button")
			.find((b) => b.text() === "Skip all")
			.trigger("click");
		await flushPromises();
		expect(w.find("#sheet-PA-S1-r3-edit").exists()).toBe(false);
	});

	it("bulk: Skip all and Create all act on the whole section", async () => {
		const w = await mountSheet();
		const items = section(w, "item");
		await items
			.findAll("button")
			.find((b) => b.text() === "Skip all")
			.trigger("click");
		expect([3, 4, 5].map((i) => pressed(w, i))).toEqual(["Skip", "Skip", "Skip"]);
		expect(pressed(w, 0)).toBe("Create");
		await items
			.findAll("button")
			.find((b) => b.text() === "Create all")
			.trigger("click");
		expect([3, 4, 5].map((i) => pressed(w, i))).toEqual(["Create", "Create", "Apply"]);
	});

	it("warns before a skip cascades, transitively, and sends each record's own choice", async () => {
		const w = await mountSheet();
		expect(row(w, 1).text()).toContain("Needs #1");
		await choice(w, 0, "Skip").trigger("click");
		expect(row(w, 0).text()).toContain(
			"Skipping this also skips 2 records that depend on it."
		);
		expect(row(w, 1).text()).toContain("Also skipped: it needs #1, which you're skipping.");
		expect(row(w, 2).text()).toContain("Needs #2");
		expect(row(w, 2).text()).toContain("Also skipped: it needs #2, which is skipped with #1.");
		expect(w.text()).toContain("Applying also skips 2 records that need a skipped one.");
		await choice(w, 1, "Use existing").trigger("click");
		await flushPromises();
		expect(row(w, 0).text()).not.toContain("Skipping this also skips");
		expect(w.text()).not.toContain("Applying also skips");
		await choice(w, 1, "Create").trigger("click");
		await answerAll(w);
		api.applySheet.mockResolvedValue({ ok: true, applying: true });
		await apply(w).trigger("click");
		const sent = api.applySheet.mock.calls[0][1].records;
		// the server skips what needs a skipped record, and says so in the outcome
		expect([sent[0], sent[1], sent[2]]).toEqual([
			{ action: "skip" },
			{ action: "create" },
			{ action: "create" },
		]);
	});

	it("answering Skip this file skips every record: none gates, all are sent skipped", async () => {
		const w = await mountSheet(flagged());
		await button(w, "Yes").trigger("click");
		expect(apply(w).attributes("disabled")).toBeDefined();
		await buttons(w, "Skip this file")[0].trigger("click"); // the routing answer
		expect(w.text()).toContain("You chose Skip this file: nothing on this sheet is created.");
		expect(apply(w).attributes("disabled")).toBeUndefined();
		expect(section(w, "item").text()).not.toContain("attention");
		api.applySheet.mockResolvedValue({ ok: true, applying: true });
		await apply(w).trigger("click");
		const sent = api.applySheet.mock.calls[0][1];
		expect(Object.values(sent.records)).toEqual(Array(7).fill({ action: "skip" }));
		expect(sent.answers["AR-2"]).toEqual({ text: "Skip this file", approve: 1 });
	});

	describe("gating", () => {
		it("Apply stays disabled, with the reason, until questions are answered", async () => {
			const w = await mountSheet();
			expect(apply(w).attributes("disabled")).toBeDefined();
			const reason = w.find("#" + apply(w).attributes("aria-describedby"));
			expect(reason.text()).toContain("Before you apply: answer 2 questions");
			await apply(w).trigger("click");
			expect(api.applySheet).not.toHaveBeenCalled();
			await answerAll(w);
			expect(apply(w).attributes("disabled")).toBeUndefined();
			expect(apply(w).attributes("aria-describedby")).toBeUndefined();
		});

		it("a routing question takes its options only; the others also take a note", async () => {
			const w = await mountSheet();
			const q1 = w.find("#sheet-PA-S1-q-AR-1");
			const q2 = w.find("#sheet-PA-S1-q-AR-2");
			expect(q1.find("textarea").exists()).toBe(true);
			expect(q2.find("textarea").exists()).toBe(false);
			expect(q1.find(".prose").html()).toContain("<strong>Freight</strong>");
			await button(w, "No").trigger("click");
			expect(button(w, "No").attributes("aria-pressed")).toBe("true");
			expect(button(w, "Yes").attributes("aria-pressed")).toBe("false");
			await button(w, "No").trigger("click"); // a second click clears it
			expect(button(w, "No").attributes("aria-pressed")).toBe("false");
		});

		it("a flagged record blocks until fixed; Set for all fixes a section at once", async () => {
			const w = await mountSheet(flagged());
			await answerAll(w);
			expect(apply(w).attributes("disabled")).toBeDefined();
			expect(w.text()).toContain(
				"fix or skip 1 record · fill the missing values on 1 record"
			);
			expect(row(w, 4).text()).toContain("Needs a fix: Enter a valid code.");
			expect(row(w, 3).text()).toContain("Missing: HSN/SAC");
			await flushPromises();
			expect(core.getDoctypeFormMeta).toHaveBeenCalledTimes(1); // to read the fix
			const offer = button(w, "Set HSN/SAC for all 2");
			expect(offer.attributes("aria-expanded")).toBe("false");
			await offer.trigger("click");
			await flushPromises();
			expect(offer.attributes("aria-expanded")).toBe("true");
			const input = w.find("#" + offer.attributes("aria-controls") + "-input");
			expect(document.activeElement).toBe(input.element);
			await input.setValue("3304");
			await button(w, "Set for 2 records").trigger("click");
			await flushPromises();
			expect(document.activeElement.textContent.trim()).toBe("Set HSN/SAC on 2 records.");
			expect(pressed(w, 3)).toBe("Edit");
			expect(pressed(w, 4)).toBe("Edit");
			expect(apply(w).attributes("disabled")).toBeUndefined();
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			const sent = api.applySheet.mock.calls[0][1].records;
			expect(sent[3]).toEqual({ action: "edit", values: { gst_hsn_code: "3304" } });
			expect(sent[4]).toEqual({ action: "edit", values: { gst_hsn_code: "3304" } });
		});

		// FE-1: SheetDetail, SheetRecord, SheetQuestions and SetForAll all read copy
		// through __(), so a page translator (window.__) reaches every one of them.
		it("FE-1: routes copy through a page translator across the sheet, its records, questions and Set for all", async () => {
			window.__ = (text, args = []) =>
				"[t] " +
				String(text).replace(/\{(\d+)\}/g, (m, i) =>
					args[i] != null ? String(args[i]) : m
				);
			try {
				const w = await mountSheet(flagged());
				await flushPromises();
				expect(w.text()).toContain(
					"[t] Jarvis needs these records and answers before it drafts this document. Nothing is created until you apply."
				);
				expect(row(w, 4).text()).toContain("[t] Needs a fix: Enter a valid code.");
				expect(row(w, 3).text()).toContain("[t] Missing: HSN/SAC");
				expect(w.text()).toContain("[t] Questions");
				expect(buttons(w, "[t] Set HSN/SAC for all 2")).toHaveLength(1);
			} finally {
				delete window.__;
			}
		});

		it("reads a fix's field from the doctype when two records need the same fix", async () => {
			const d = detail();
			d.records[3].needs_fix = HSN_FIX;
			d.records[4].needs_fix = HSN_FIX;
			const w = await mountSheet(d);
			await flushPromises();
			expect(core.getDoctypeFormMeta).toHaveBeenCalledWith("Item");
			expect(core.getDoctypeFormMeta).toHaveBeenCalledTimes(1);
			expect(button(w, "Set HSN/SAC for all 2")).toBeTruthy();
			await choice(w, 3, "Skip").trigger("click");
			expect(button(w, "Set HSN/SAC for all 2")).toBeFalsy();
		});

		it("Escape closes Set for all, back on its button", async () => {
			const w = await mountSheet(flagged());
			await flushPromises();
			const offer = button(w, "Set HSN/SAC for all 2");
			await offer.trigger("click");
			await flushPromises();
			const panel = w.find("#" + offer.attributes("aria-controls"));
			await panel.find("input").trigger("keydown", { key: "Escape" });
			await flushPromises();
			expect(w.find("#" + offer.attributes("aria-controls")).exists()).toBe(false);
			expect(offer.attributes("aria-expanded")).toBe("false");
			expect(document.activeElement).toBe(offer.element);
		});

		it("Show the first takes focus to the first record that blocks", async () => {
			const w = await mountSheet(flagged());
			await button(w, "Show the first").trigger("click");
			await flushPromises();
			expect(document.activeElement).toBe(row(w, 3).element);
		});
	});

	it("Use existing loads its candidates lazily and sends the pick", async () => {
		api.getSheetCandidates.mockResolvedValue({
			0: [{ name: "SUP-7", title: "Acme Pvt", gstin: "27AAACA" }],
		});
		const w = await mountSheet();
		await answerAll(w);
		expect(api.getSheetCandidates).not.toHaveBeenCalled();
		const use = choice(w, 0, "Use existing");
		await use.trigger("click");
		await flushPromises();
		expect(api.getSheetCandidates).toHaveBeenCalledWith("PA-S1", [0]);
		expect(use.attributes("aria-expanded")).toBe("true");
		const panel = w.find("#" + use.attributes("aria-controls"));
		expect(document.activeElement).toBe(panel.element);
		expect(panel.text()).toContain("27AAACA");
		expect(apply(w).attributes("disabled")).toBeDefined(); // nothing picked yet
		await panel.find('[aria-label="Use Acme Pvt"]').trigger("click");
		await flushPromises();
		expect(row(w, 0).text()).toContain("Uses the existing Supplier Acme Pvt (SUP-7)");
		expect(document.activeElement).toBe(use.element);
		// the Link search is the other way in
		await choice(w, 6, "Use existing").trigger("click");
		await flushPromises();
		expect(api.getSheetCandidates).toHaveBeenLastCalledWith("PA-S1", [6]);
		expect(row(w, 6).text()).toContain("No matching UOM found");
		row(w, 6)
			.findComponent(Autocomplete)
			.vm.$emit("update:modelValue", { value: "Nos", label: "Nos" });
		await flushPromises();
		expect(row(w, 6).text()).toContain("Uses the existing UOM Nos");
		api.applySheet.mockResolvedValue({ ok: true, applying: true });
		await apply(w).trigger("click");
		const sent = api.applySheet.mock.calls[0][1].records;
		expect(sent[0]).toEqual({ action: "use_existing", existing: "SUP-7" });
		expect(sent[6]).toEqual({ action: "use_existing", existing: "Nos" });
		expect(api.getSheetCandidates).toHaveBeenCalledTimes(2); // cached per record
	});

	it("a new version of the sheet remounts its rows and asks for candidates again", async () => {
		api.getSheetCandidates.mockResolvedValue({ 0: [{ name: "SUP-7", title: "Acme Pvt" }] });
		const w = await mountSheet();
		await choice(w, 0, "Use existing").trigger("click");
		await flushPromises();
		expect(w.find("#sheet-PA-S1-r0-existing").exists()).toBe(true);
		api.getPendingAction.mockResolvedValue(detail({ card_sha256: "sha-2" }));
		w.vm.refresh();
		await flushPromises();
		expect(w.find("#sheet-PA-S1-r0-existing").exists()).toBe(false);
		expect(pressed(w, 0)).toBe("Use existing");
		await choice(w, 0, "Use existing").trigger("click");
		await flushPromises();
		expect(api.getSheetCandidates).toHaveBeenCalledTimes(2);
	});

	it("Edit opens the form inline: locked fields read-only, Escape closes it", async () => {
		const d = flagged();
		const w = await mountSheet(d);
		const edit = choice(w, 3, "Edit");
		await edit.trigger("click");
		await flushPromises();
		expect(core.getDoctypeFormMeta).toHaveBeenCalledWith("Item");
		const panel = w.find("#" + edit.attributes("aria-controls"));
		expect(edit.attributes("aria-pressed")).toBe("true");
		expect(edit.attributes("aria-expanded")).toBe("true");
		const label = panel.findAll("label").find((l) => l.text().startsWith("HSN/SAC"));
		const input = panel.find("#" + label.attributes("for"));
		expect(input.attributes("aria-invalid")).toBe("true"); // highlighted: missing
		await input.setValue("3304");
		expect(row(w, 3).text()).not.toContain("Missing:");
		await panel.trigger("keydown", { key: "Escape" });
		await flushPromises();
		expect(w.find("#" + edit.attributes("aria-controls")).exists()).toBe(false);
		expect(document.activeElement).toBe(edit.element);
		expect(pressed(w, 3)).toBe("Edit"); // closing keeps the edit
	});

	it("the Supplier form shows its identifying field read-only", async () => {
		core.getDoctypeFormMeta.mockResolvedValue({
			ok: true,
			fields: [
				{ fieldname: "supplier_name", label: "Supplier Name", fieldtype: "Data" },
				{ fieldname: "tax_id", label: "Tax ID", fieldtype: "Data" },
			],
			tables: {},
		});
		const w = await mountSheet();
		await choice(w, 0, "Edit").trigger("click");
		await flushPromises();
		const label = row(w, 0)
			.findAll("label")
			.find((l) => l.text() === "Tax ID");
		expect(w.find("#" + label.attributes("for")).element.tagName).toBe("DIV");
	});

	describe("apply", () => {
		it("sends every decision with the sheet's sha and count, then shows progress", async () => {
			const w = await mountSheet();
			await answerAll(w);
			await w.find("#sheet-PA-S1-q-AR-1 textarea").setValue("only this month");
			api.applySheet.mockResolvedValue({ ok: true, applying: true, pa_status: "Executing" });
			await apply(w).trigger("click");
			await flushPromises();
			expect(api.applySheet).toHaveBeenCalledWith("PA-S1", {
				records: {
					0: { action: "create" },
					1: { action: "create" },
					2: { action: "create" },
					3: { action: "create" },
					4: { action: "create" },
					5: { action: "apply" },
					6: { action: "create" },
				},
				answers: {
					"AR-1": { text: "Yes: only this month", approve: 1 },
					"AR-2": { text: "Purchase invoice", approve: 1 },
				},
				expected_card_sha256: "sha-1",
				record_count: 7,
			});
			expect(onChanged).toHaveBeenCalledTimes(1);
			expect(onDecided).not.toHaveBeenCalled();
			expect(w.find('[role="status"]').text()).toBe("Applying the sheet… 0 of 7");
			expect(document.activeElement).toBe(w.find('[role="status"]').element);
			expect(w.find('[role="progressbar"]').attributes("aria-valuemax")).toBe("7");
			expect(apply(w)).toBeFalsy();
		});

		it("is busy while applying: a second click sends nothing", async () => {
			const w = await mountSheet();
			await answerAll(w);
			let resolve;
			api.applySheet.mockReturnValue(new Promise((r) => (resolve = r)));
			await apply(w).trigger("click");
			expect(apply(w).attributes("disabled")).toBeDefined();
			expect(skipFile(w).attributes("disabled")).toBeDefined();
			await apply(w).trigger("click");
			resolve({ ok: false, reason_code: "busy" });
			await flushPromises();
			expect(api.applySheet).toHaveBeenCalledTimes(1);
		});

		it.each([
			["busy", "Someone is acting on this sheet right now. Try again in a moment."],
			["unavailable", "The apply couldn't start. Nothing was created. Try again."],
			[
				"identity_refused",
				"This can no longer run as the person who dropped the file. Skip this file, or ask them to drop it again.",
			],
			["armed_run", "This run is stopping, so nothing was applied."],
			["collecting", "Jarvis is still listing this sheet. Apply it once the run pauses."],
		])("a %s refusal keeps the sheet and says why", async (reason_code, text) => {
			const w = await mountSheet();
			await answerAll(w);
			api.applySheet.mockResolvedValue({ ok: false, reason_code });
			await apply(w).trigger("click");
			await flushPromises();
			expect(w.find('[role="alert"]').text()).toBe(text);
			expect(onDecided).not.toHaveBeenCalled();
			expect(pressed(w, 0)).toBe("Create");
		});

		it("a load in flight when Apply starts never undoes it", async () => {
			const w = await mountSheet();
			await answerAll(w);
			let stale;
			api.getPendingAction.mockReturnValueOnce(new Promise((r) => (stale = r)));
			w.vm.refresh();
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			await flushPromises();
			stale(detail());
			await flushPromises();
			expect(apply(w)).toBeFalsy();
			expect(w.find('[role="status"]').text()).toBe("Applying the sheet… 0 of 7");
		});

		it("an executing refusal shows the apply already running", async () => {
			const w = await mountSheet();
			await answerAll(w);
			api.applySheet.mockResolvedValue({
				ok: false,
				reason_code: "executing",
				pa_status: "Executing",
			});
			api.getPendingAction.mockResolvedValue(
				detail({ status: "Executing", can_act: 0, progress: { done: 2, total: 7 } })
			);
			await apply(w).trigger("click");
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
			expect(w.find('[role="status"]').text()).toBe("Applying the sheet… 2 of 7");
			expect(w.find('[role="alert"]').text()).toBe("This sheet is being applied right now.");
			expect(onDecided).not.toHaveBeenCalled();
		});

		it.each([
			["already_handled", "This sheet was already handled."],
			["not_found", "This sheet is no longer available."],
		])("a %s refusal leaves the lane", async (reason_code, text) => {
			const w = await mountSheet();
			await answerAll(w);
			api.applySheet.mockResolvedValue({ ok: false, reason_code });
			await apply(w).trigger("click");
			await flushPromises();
			expect(toast.error).toHaveBeenCalledWith(text);
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("a changed sheet reloads, keeping the choices that still fit", async () => {
			const w = await mountSheet();
			await answerAll(w);
			await choice(w, 6, "Skip").trigger("click");
			api.applySheet.mockResolvedValue({ ok: false, reason_code: "changed" });
			api.getPendingAction.mockResolvedValue(detail({ card_sha256: "sha-2" }));
			await apply(w).trigger("click");
			await flushPromises();
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
			expect(w.find('[role="alert"]').text()).toBe(
				"This sheet changed since you opened it, so it was reloaded. Review it and apply again."
			);
			expect(pressed(w, 6)).toBe("Skip");
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			expect(api.applySheet.mock.calls[1][1].expected_card_sha256).toBe("sha-2");
		});

		it("FE-2: a bounced reload keeps the chosen existing record's title, not just its id", async () => {
			const w = await mountSheet();
			await answerAll(w);
			api.getSheetCandidates.mockResolvedValue({
				0: [{ name: "SUP-9", title: "Acme Corp" }],
			});
			await choice(w, 0, "Use existing").trigger("click");
			await flushPromises();
			await button(w, "Use this").trigger("click");
			expect(row(w, 0).text()).toContain("Uses the existing Supplier Acme Corp (SUP-9)");
			api.applySheet.mockResolvedValue({ ok: false, reason_code: "changed" });
			api.getPendingAction.mockResolvedValue(
				detail({
					card_sha256: "sha-2",
					decisions: { records: { 0: { action: "use_existing", existing: "SUP-9" } } },
				})
			);
			await apply(w).trigger("click");
			await flushPromises();
			expect(pressed(w, 0)).toBe("Use existing");
			expect(row(w, 0).text()).toContain("Uses the existing Supplier Acme Corp (SUP-9)");
		});

		it("an invalid apply shows each error in place and focuses the first", async () => {
			const w = await mountSheet();
			await answerAll(w);
			api.applySheet.mockResolvedValue({
				ok: false,
				reason_code: "invalid",
				errors: { 6: "That UOM was not found.", 4: "Item Name is too long." },
				answer_errors: { "AR-1": "Keep the answer under 1000 characters." },
			});
			await apply(w).trigger("click");
			await flushPromises();
			const banner = w.find('[role="alert"]');
			expect(banner.text()).toBe(
				"Nothing was applied: 2 records and 1 answer need attention."
			);
			expect(row(w, 4).text()).toContain("Item Name is too long.");
			expect(row(w, 6).text()).toContain("That UOM was not found.");
			expect(w.find("#sheet-PA-S1-q-AR-1").text()).toContain("Keep the answer under");
			expect(document.activeElement).toBe(row(w, 4).element);
			expect(row(w, 4).attributes("aria-describedby")).toContain("sheet-PA-S1-r4-error");
			expect(section(w, "item").text()).toContain("1 needs attention");
			expect(section(w, "other").text()).toContain("1 needs attention");
		});
	});

	describe("while applying and after", () => {
		async function applying(d = detail()) {
			const w = await mountSheet(d);
			await answerAll(w);
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			await flushPromises();
			return w;
		}

		it("follows sheet:progress, then keeps the outcome in view until Done", async () => {
			const w = await applying();
			socket.emit({
				kind: "sheet:progress",
				name: "PA-OTHER",
				state: "applying",
				done: 3,
				total: 9,
			});
			socket.emit({
				kind: "sheet:progress",
				name: "PA-S1",
				state: "applying",
				done: 4,
				total: 7,
			});
			await flushPromises();
			expect(w.find('[role="status"]').text()).toBe("Applying the sheet… 4 of 7");
			expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("4");
			api.getPendingAction.mockResolvedValue(
				detail({
					status: "Executed",
					can_act: 0,
					outcome: {
						records: [
							{
								index: 0,
								doctype: "Supplier",
								title: "Acme",
								action: "created",
								name: "SUP-1",
							},
							{ index: 6, doctype: "UOM", title: "Box", action: "skipped" },
						],
						answers: [],
					},
				})
			);
			socket.emit({
				kind: "sheet:progress",
				name: "PA-S1",
				state: "applied",
				done: 7,
				total: 7,
			});
			await flushPromises();
			expect(toast.success).toHaveBeenCalledWith(
				"Applied: created 1 · skipped 1. The run continues."
			);
			expect(onChanged).toHaveBeenCalledTimes(2); // the rail lets the row go
			expect(onDecided).not.toHaveBeenCalled();
			expect(w.text()).toContain("Created Supplier Acme as SUP-1");
			const status = w.find('[role="status"]');
			expect(status.text()).toBe("Applied.");
			expect(document.activeElement).toBe(status.element);
			await button(w, "Done").trigger("click");
			expect(onDecided).toHaveBeenCalledTimes(1);
			expect(button(w, "Done")).toBeFalsy();
			w.vm.refresh();
			await flushPromises();
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("leaving an applied sheet before Done still reports it, once", async () => {
			const w = await applying();
			api.getPendingAction.mockResolvedValue(
				detail({ status: "Executed", can_act: 0, outcome: { records: [], answers: [] } })
			);
			socket.emit({ kind: "sheet:progress", name: "PA-S1", state: "applied" });
			await flushPromises();
			expect(onDecided).not.toHaveBeenCalled();
			w.unmount();
			mounted = [];
			expect(onDecided).toHaveBeenCalledTimes(1);
		});

		it("polls the slim status while applying; a full reload only when it moves", async () => {
			vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
			socket = null;
			const w = await applying();
			expect(api.getPendingAction).toHaveBeenCalledTimes(1);
			// same status and sha: a slim tick patches progress in place, no reload
			api.getSheetStatus.mockResolvedValue({
				name: "PA-S1",
				status: "Executing",
				collecting: 0,
				progress: { done: 3, total: 7 },
				card_sha256: "sha-1",
				record_count: 7,
				counts_line: "3 of 7",
			});
			await vi.advanceTimersByTimeAsync(5000);
			expect(api.getSheetStatus).toHaveBeenCalledTimes(1);
			expect(api.getPendingAction).toHaveBeenCalledTimes(1);
			expect(w.find('[role="status"]').text()).toBe("Applying the sheet… 3 of 7");
			// the status moved: a full reload fetches the terminal detail
			api.getSheetStatus.mockResolvedValue({
				name: "PA-S1",
				status: "Discarded",
				collecting: 0,
				progress: null,
				card_sha256: "sha-1",
				record_count: 7,
				counts_line: "",
			});
			api.getPendingAction.mockResolvedValue(
				detail({ status: "Discarded", can_act: 0, reason_code: "discarded" })
			);
			await vi.advanceTimersByTimeAsync(5000);
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
			expect(onDecided).toHaveBeenCalledTimes(1);
			await vi.advanceTimersByTimeAsync(20000);
			expect(api.getSheetStatus).toHaveBeenCalledTimes(2); // ended: no more polls
			expect(api.getPendingAction).toHaveBeenCalledTimes(2);
		});

		it("returned with errors: shows them, focuses the first, keeps the decisions", async () => {
			const w = await applying();
			api.getPendingAction.mockResolvedValue(
				detail({
					errors: {
						2: "Blocked by #2: it needs that record.",
						1: "Address Line 1 is required.",
					},
					decisions: {
						records: {
							0: { action: "use_existing", existing: "SUP-9" },
							6: { action: "skip" },
						},
						answers: { "AR-1": { text: "No" }, "AR-2": { text: "Purchase invoice" } },
					},
				})
			);
			socket.emit({
				kind: "sheet:progress",
				name: "PA-S1",
				state: "returned",
				done: 0,
				total: 7,
			});
			await flushPromises();
			expect(onDecided).not.toHaveBeenCalled();
			expect(w.find('[role="alert"]').text()).toBe(
				"Nothing was applied: 2 records need attention."
			);
			expect(row(w, 2).text()).toContain("Blocked by #2: it needs that record.");
			expect(document.activeElement).toBe(row(w, 1).element);
			expect(pressed(w, 0)).toBe("Use existing");
			expect(row(w, 0).text()).toContain("Uses the existing Supplier SUP-9");
			expect(pressed(w, 6)).toBe("Skip");
			expect(button(w, "No").attributes("aria-pressed")).toBe("true");
			expect(apply(w).attributes("disabled")).toBeUndefined();
		});

		it("after a bounce, un-skipping a record brings back its dependents' edits", async () => {
			const w = await mountSheet();
			await answerAll(w);
			await choice(w, 1, "Edit").trigger("click");
			await flushPromises();
			const panel = w.find("#sheet-PA-S1-r1-edit");
			const label = panel.findAll("label").find((l) => l.text().startsWith("Item Name"));
			await panel.find("#" + label.attributes("for")).setValue("Acme Billing");
			await choice(w, 0, "Skip").trigger("click");
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			await flushPromises();
			const sent = api.applySheet.mock.calls[0][1];
			expect(sent.records[1]).toEqual({
				action: "edit",
				values: { item_name: "Acme Billing" },
			});
			api.getPendingAction.mockResolvedValue(
				detail({
					errors: { 6: "That UOM was not found." },
					decisions: { records: sent.records, answers: sent.answers },
				})
			);
			socket.emit({ kind: "sheet:progress", name: "PA-S1", state: "returned" });
			await flushPromises();
			expect(pressed(w, 1)).toBe("Edit");
			await choice(w, 0, "Create").trigger("click");
			expect(row(w, 1).text()).not.toContain("Also skipped");
			api.applySheet.mockClear();
			await apply(w).trigger("click");
			expect(api.applySheet.mock.calls[0][1].records[1]).toEqual({
				action: "edit",
				values: { item_name: "Acme Billing" },
			});
		});

		it("an older bounce that sent the cascade as skips leaves it to the cascade", async () => {
			const w = await mountSheet(
				detail({
					errors: { 6: "That UOM was not found." },
					decisions: {
						records: {
							0: { action: "skip" },
							1: { action: "skip" },
							2: { action: "skip" },
						},
					},
				})
			);
			expect(row(w, 1).text()).toContain("Also skipped");
			await choice(w, 0, "Create").trigger("click");
			expect([0, 1, 2].map((i) => pressed(w, i))).toEqual(["Create", "Create", "Create"]);
		});

		it("never reloads after the board let it go", async () => {
			const w = await applying();
			w.unmount();
			mounted = [];
			expect(socket.handlers.size).toBe(0);
			expect(api.getPendingAction).toHaveBeenCalledTimes(1);
		});
	});

	describe("read-only states", () => {
		it("an applied sheet opened later shows its outcome and decides nothing", async () => {
			const w = await mountSheet(
				detail({
					status: "Executed",
					can_act: 0,
					outcome: {
						records: [
							{
								index: 0,
								doctype: "Supplier",
								title: "Acme",
								action: "existing",
								name: "SUP-1",
								auto: 1,
							},
							{
								index: 1,
								doctype: "Address",
								title: "Acme-B",
								action: "skipped",
								cascade: 1,
							},
						],
						answers: [{ name: "AR-1", title: "Freight", answer: "Yes" }],
					},
				})
			);
			expect(w.find('[role="status"]').text()).toBe("Applied.");
			expect(w.text()).toContain("Used 1 existing · skipped 1");
			expect(w.text()).toContain(
				"Used the existing Supplier SUP-1 for Acme (it already existed)"
			);
			expect(w.text()).toContain("Skipped Address Acme-B (it needed a skipped record)");
			expect(w.text()).toContain("Freight");
			expect(apply(w)).toBeFalsy();
			expect(onDecided).not.toHaveBeenCalled();
		});

		it("a skipped sheet and a stuck one say what happened", async () => {
			const skipped = await mountSheet(
				detail({ status: "Discarded", can_act: 0, reason_code: "discarded" })
			);
			expect(skipped.find('[role="status"]').text()).toBe(
				"Skipped. Nothing from this sheet was created."
			);
			const stuck = await mountSheet(
				detail({
					status: "Failed",
					can_act: 0,
					reason_code: "stuck",
					reason: "The approval sheet got stuck while it was still being listed; nothing was changed.",
				})
			);
			expect(stuck.find('[role="status"]').text()).toContain("got stuck");
		});
	});

	it("Skip this file asks first, then discards and leaves the lane", async () => {
		const w = await mountSheet();
		api.discardSheet.mockResolvedValue({ ok: true, reason_code: "discarded" });
		await skipFile(w).trigger("click");
		await flushPromises();
		expect(confirmDialog).toHaveBeenCalledWith(
			expect.objectContaining({ title: "Skip this file?" })
		);
		expect(api.discardSheet).toHaveBeenCalledWith("PA-S1");
		expect(toast.success).toHaveBeenCalledWith("Skipped this file. Nothing was created.");
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("refresh never wipes the choices on screen", async () => {
		const w = await mountSheet();
		await choice(w, 6, "Skip").trigger("click");
		w.vm.refresh();
		await flushPromises();
		expect(api.getPendingAction).toHaveBeenCalledTimes(2);
		expect(pressed(w, 6)).toBe("Skip");
	});

	describe("a 250-record sheet", () => {
		function big() {
			const recs = [records()[0], records()[1]];
			for (let i = 2; i < 250; i++) recs.push(record(i));
			recs[199].needs_fix = "Item Group is required";
			return detail({ records: recs, questions: [], record_count: 250 });
		}

		it("shows 20 items, keeps Apply gated on a hidden record, and bulk-skips them all", async () => {
			const w = await mountSheet(big());
			expect(w.findAll("li[id^='sheet-PA-S1-r']")).toHaveLength(22);
			expect(button(w, "Show all 248")).toBeTruthy();
			expect(apply(w).attributes("disabled")).toBeDefined();
			expect(w.text()).toContain("Before you apply: fix or skip 1 record");
			await button(w, "Show the first").trigger("click");
			await flushPromises();
			expect(document.activeElement).toBe(row(w, 199).element);
			expect(button(w, "Show all 248")).toBeFalsy();
			await section(w, "item")
				.findAll("button")
				.find((b) => b.text() === "Skip all")
				.trigger("click");
			expect(apply(w).attributes("disabled")).toBeUndefined();
			api.applySheet.mockResolvedValue({ ok: true, applying: true });
			await apply(w).trigger("click");
			const sent = api.applySheet.mock.calls[0][1];
			expect(Object.keys(sent.records)).toHaveLength(250);
			expect(Object.values(sent.records).filter((e) => e.action === "skip")).toHaveLength(
				248
			);
			expect(sent.record_count).toBe(250);
		});

		it("Show all moves focus to the first record it reveals", async () => {
			const w = await mountSheet(big());
			await button(w, "Show all 248").trigger("click");
			await flushPromises();
			expect(w.findAll("li[id^='sheet-PA-S1-r']")).toHaveLength(250);
			expect(document.activeElement).toBe(row(w, 22).element);
		});
	});
});
