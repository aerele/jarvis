import { describe, it, expect } from "vitest";
import {
	actionsFor,
	answerText,
	applyPatch,
	blockers,
	buildDecisions,
	cascade,
	cascadeNote,
	cascadeReason,
	fieldNamedIn,
	fixOf,
	initialAnswers,
	initialChoices,
	isSheetSettled,
	keyFields,
	outcomeSummary,
	progressText,
	reachFrom,
	sections,
	setForAllGroups,
	sheetRefusal,
	sheetStatusLine,
	sheetTitle,
	skipsFile,
	splitErrors,
} from "./sheet";

const HSN_FIX = "HSN/SAC Code is required. Please enter a valid HSN/SAC code.";
// the server names the field; its message need not name the label
const HSN_FLAG = { field: "gst_hsn_code", label: "HSN/SAC", message: "Enter a valid code." };
const HSN_FIELD = {
	fieldname: "gst_hsn_code",
	label: "HSN/SAC",
	fieldtype: "Link",
	options: "GST HSN Code",
};
const need = (index, fieldname = "gst_hsn_code", label = "HSN/SAC") => ({
	doc_index: index,
	doctype: "Item",
	fieldname,
	label,
	field: { ...HSN_FIELD, fieldname, label },
});

const rec = (index, over = {}) => ({
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

function sheet() {
	return [
		rec(0, {
			doctype: "Supplier",
			category: "party",
			title: "Acme",
			values: { supplier_name: "Acme", tax_id: "27AAACA", bank_key: "••••••" },
			secret: ["bank_key"],
		}),
		rec(1, {
			doctype: "Address",
			category: "address",
			title: "Acme-Billing",
			depends_on: [0],
		}),
		rec(2, { doctype: "Contact", category: "address", title: "Asha", depends_on: [1] }),
		rec(3, { needs_input: [need(3)] }),
		rec(4, { needs_fix: HSN_FLAG }),
		rec(5, { op: "update", name: "ITEM-5", title: "ITEM-5" }),
		rec(6, { doctype: "UOM", category: "other", title: "Box" }),
	];
}

const questions = () => [
	{
		name: "AR-1",
		question: "Book freight?",
		options: ["Yes", "No"],
		routing: "",
		status: "Pending",
	},
	{
		name: "AR-2",
		question: "Which skill?",
		options: ["Purchase invoice", "Skip this file"],
		routing: "skill_conflict",
		status: "Pending",
	},
	{ name: "AR-3", question: "Anything else?", options: [], routing: "", status: "Pending" },
	{ name: "AR-4", question: "Old one", options: [], routing: "", status: "Approved" },
];

describe("sheet helpers", () => {
	it("titles a sheet by its file, else its chat, never the summary", () => {
		const t = {
			file_name: "inv.pdf",
			conversation_title: "Chat",
			summary: "Approval sheet: x",
		};
		expect(sheetTitle(t)).toBe("inv.pdf");
		expect(sheetTitle({ ...t, file_name: "" })).toBe("Chat");
		expect(sheetTitle({ summary: "Approval sheet: loreal.pdf" })).toBe("Approval sheet");
		expect(sheetTitle(null)).toBe("Approval sheet");
	});

	it("reads a fix, structured or a legacy text", () => {
		expect(fixOf(rec(0, { needs_fix: HSN_FLAG }))).toEqual(HSN_FLAG);
		expect(fixOf(rec(0, { needs_fix: { message: "Bad" } }))).toEqual({
			field: null,
			label: null,
			message: "Bad",
		});
		expect(fixOf(rec(0, { needs_fix: "Bad email" }))).toEqual({
			field: null,
			label: null,
			message: "Bad email",
		});
		expect(fixOf(rec(0, { needs_fix: null }))).toBeNull();
		expect(fixOf(rec(0, { needs_fix: "" }))).toBeNull();
	});

	it("groups records into the four sections in order, empty ones left out", () => {
		const s = sections(sheet());
		expect(s.map((x) => x.key)).toEqual(["party", "address", "item", "other"]);
		expect(s.map((x) => x.label)).toEqual([
			"Supplier",
			"Address / Contact",
			"Items",
			"Other masters",
		]);
		expect(s[2].records.map((r) => r.index)).toEqual([3, 4, 5]);
		expect(s[2].updatesOnly).toBe(false);
		expect(sections([rec(0, { op: "update" })])[0].updatesOnly).toBe(true);
		expect(sections([])).toEqual([]);
	});

	it("offers create actions to a new record and apply actions to an update", () => {
		expect(actionsFor(rec(0))).toEqual(["create", "edit", "use_existing", "skip"]);
		expect(actionsFor(rec(0, { op: "update" }))).toEqual(["apply", "edit", "skip"]);
	});

	it("shows key fields as text, never secrets, tables or the title again", () => {
		const r = sheet()[0];
		r.values.addresses = [{ city: "Pune" }];
		expect(keyFields(r)).toEqual([{ key: "tax_id", label: "Tax id", value: "27AAACA" }]);
	});

	it("patches tables by position", () => {
		const values = { a: "1", rows: [{ x: 1, y: 2 }, { x: 3 }] };
		expect(applyPatch(values, { a: "2", rows: [{}, { y: 9 }] })).toEqual({
			a: "2",
			rows: [
				{ x: 1, y: 2 },
				{ x: 3, y: 9 },
			],
		});
		expect(values.rows[1]).toEqual({ x: 3 });
	});

	it("defaults every record, and prefills a bounced apply's decisions", () => {
		const records = sheet();
		expect(initialChoices(records)[5]).toEqual({
			action: "apply",
			values: null,
			existing: "",
		});
		const c = initialChoices(records, {
			records: {
				0: { action: "use_existing", existing: "SUP-1" },
				3: { action: "edit", values: { gst_hsn_code: "3304" } },
				5: { action: "use_existing" },
				9: { action: "skip" },
			},
		});
		expect(c[0]).toEqual({ action: "use_existing", values: null, existing: "SUP-1" });
		expect(c[3].values).toEqual({ ...records[3].values, gst_hsn_code: "3304" });
		expect(c[5].action).toBe("apply"); // not an update's action
		expect(c[9]).toBeUndefined();
	});

	it("a bounced skip under another skip takes back its own choice", () => {
		const records = sheet();
		const before = initialChoices(records);
		before[1] = {
			action: "edit",
			values: { ...records[1].values, city: "Pune" },
			existing: "",
		};
		// an older apply sent what a skip cascades to as skipped
		const old = {
			records: { 0: { action: "skip" }, 1: { action: "skip" }, 2: { action: "skip" } },
		};
		const c = initialChoices(records, old, before);
		expect(c[0].action).toBe("skip");
		expect(c[1]).toEqual(before[1]);
		expect(c[2].action).toBe("create");
		expect([...cascade(records, c).keys()]).toEqual([1, 2]);
		expect(initialChoices(records, old)[1].action).toBe("create");
		// a used existing record in between keeps the skip below it
		old.records[1] = { action: "use_existing", existing: "ADDR-1" };
		expect(initialChoices(records, old)[2].action).toBe("skip");
	});

	it("carries earlier choices over a reload when the decisions say nothing", () => {
		const records = sheet();
		const before = initialChoices(records);
		before[6].action = "skip";
		expect(initialChoices(records, {}, before)[6].action).toBe("skip");
	});

	it("FE-2: a bounced use_existing echo restores the label, for the same id only", () => {
		const records = sheet();
		const before = initialChoices(records);
		before[0] = {
			action: "use_existing",
			values: null,
			existing: "SUP-1",
			existingLabel: "Acme Corp (SUP-1)",
		};
		// the server echoes only the id: the in-memory label for that same id fills it in
		const same = { records: { 0: { action: "use_existing", existing: "SUP-1" } } };
		expect(initialChoices(records, same, before)[0]).toEqual({
			action: "use_existing",
			values: null,
			existing: "SUP-1",
			existingLabel: "Acme Corp (SUP-1)",
		});
		// a different id (or no prior label): no stale label carried over
		const other = { records: { 0: { action: "use_existing", existing: "SUP-2" } } };
		expect(initialChoices(records, other, before)[0]).toEqual({
			action: "use_existing",
			values: null,
			existing: "SUP-2",
		});
		expect(initialChoices(records, same)[0]).toEqual({
			action: "use_existing",
			values: null,
			existing: "SUP-1",
		});
	});

	it("prefills answers: a chip, a chip plus a note, or a note", () => {
		const a = initialAnswers(questions(), {
			answers: {
				"AR-1": { text: "Yes: only this month" },
				"AR-2": { text: "Purchase invoice" },
				"AR-3": { text: "Nothing" },
			},
		});
		expect(a["AR-1"]).toEqual({ option: "Yes", note: "only this month" });
		expect(a["AR-2"]).toEqual({ option: "Purchase invoice", note: "" });
		expect(a["AR-3"]).toEqual({ option: "", note: "Nothing" });
		expect(a["AR-4"]).toBeUndefined();
	});

	it("words an answer; a routing question takes its option only", () => {
		const [q1, q2, q3] = questions();
		expect(answerText(q1, { option: "No", note: " later " })).toBe("No: later");
		expect(answerText(q1, { option: "", note: "maybe" })).toBe("maybe");
		expect(answerText(q2, { option: "", note: "free text" })).toBe("");
		expect(answerText(q2, { option: "Skip this file", note: "x" })).toBe("Skip this file");
		expect(answerText(q3, { option: "", note: "  " })).toBe("");
	});

	describe("cascade", () => {
		it("skips every record that needs a skipped one, transitively", () => {
			const records = sheet();
			const c = initialChoices(records);
			c[0].action = "skip";
			expect([...cascade(records, c)]).toEqual([
				[1, 0],
				[2, 0],
			]);
			expect([...reachFrom(records, c, 0)]).toEqual([1, 2]);
		});

		it("stops at a record used as existing or skipped itself", () => {
			const records = sheet();
			const c = initialChoices(records);
			c[0].action = "skip";
			c[1].action = "use_existing";
			expect(cascade(records, c).size).toBe(0);
			expect(reachFrom(records, c, 0).size).toBe(0);
			c[1].action = "skip";
			expect([...cascade(records, c).keys()]).toEqual([2]);
		});
	});

	describe("gating", () => {
		const ready = (records, c) => {
			c[3] = {
				action: "edit",
				values: { ...records[3].values, gst_hsn_code: "3304" },
				existing: "",
			};
			c[4] = {
				action: "edit",
				values: { ...records[4].values, gst_hsn_code: "3305" },
				existing: "",
			};
			return c;
		};
		const answered = () => ({
			"AR-1": { option: "Yes", note: "" },
			"AR-2": { option: "Purchase invoice", note: "" },
			"AR-3": { option: "", note: "No" },
		});

		it("names what still blocks the apply", () => {
			const records = sheet();
			const g = blockers({
				records,
				choices: initialChoices(records),
				questions: questions(),
			});
			expect(g.records).toEqual({ 3: "missing", 4: "fix" });
			expect(g.questions).toEqual(["AR-1", "AR-2", "AR-3"]);
			expect(g.summary).toBe(
				"Before you apply: answer 3 questions · fix or skip 1 record · fill the missing values on 1 record"
			);
			expect(g.first).toEqual({ type: "record", index: 3 });
		});

		it("clears once every question is answered and every flagged record handled", () => {
			const records = sheet();
			const choices = ready(records, initialChoices(records));
			const g = blockers({ records, choices, questions: questions(), answers: answered() });
			expect(g.summary).toBe("");
			expect(g.first).toBeNull();
		});

		it("a needs-fix record must change a value when edited; skip or existing also clear it", () => {
			const records = sheet();
			const choices = ready(records, initialChoices(records));
			choices[4].values = { ...records[4].values };
			const args = { records, choices, questions: [], answers: {} };
			expect(blockers(args).records).toEqual({ 4: "fix_unchanged" });
			choices[4].action = "skip";
			expect(blockers(args).summary).toBe("");
			choices[4] = { action: "use_existing", values: null, existing: "" };
			expect(blockers(args).records).toEqual({ 4: "existing" });
			choices[4].existing = "ITEM-9";
			expect(blockers(args).summary).toBe("");
		});

		it("a record skipped by the cascade never blocks", () => {
			const records = sheet();
			records[2].needs_fix = { field: "email_id", label: "Email", message: "Bad email" };
			const choices = ready(records, initialChoices(records));
			const args = { records, choices, questions: [], answers: {} };
			expect(blockers(args).records).toEqual({ 2: "fix" });
			choices[0].action = "skip";
			expect(blockers(args).summary).toBe("");
		});

		it("a legacy text fix blocks like a structured one; null never does", () => {
			const records = sheet();
			records[4].needs_fix = HSN_FIX;
			records[6].needs_fix = null;
			const g = blockers({ records, choices: initialChoices(records), questions: [] });
			expect(g.records).toEqual({ 3: "missing", 4: "fix" });
		});

		it("a routing Skip this file skips the whole file: no record blocks", () => {
			const records = sheet();
			const answers = { ...answered(), "AR-2": { option: "Skip this file", note: "" } };
			const g = blockers({
				records,
				choices: initialChoices(records),
				questions: questions(),
				answers,
			});
			expect(skipsFile(questions(), answers)).toBe(true);
			expect(g.records).toEqual({});
			expect(g.summary).toBe("");
			expect(skipsFile(questions(), answered())).toBe(false);
			// a question still open blocks
			delete answers["AR-1"];
			expect(
				blockers({ records, choices: {}, questions: questions(), answers }).questions
			).toEqual(["AR-1"]);
		});

		it("a routing question needs one of its options", () => {
			const records = [];
			const answers = { ...answered(), "AR-2": { option: "", note: "whatever" } };
			const g = blockers({ records, choices: {}, questions: questions(), answers });
			expect(g.questions).toEqual(["AR-2"]);
			expect(g.first).toEqual({ type: "question", name: "AR-2" });
		});
	});

	describe("the apply payload", () => {
		it("sends every record, only the edited values, the answers, the sha and the count", () => {
			const records = sheet();
			const choices = initialChoices(records);
			choices[0] = { action: "use_existing", values: null, existing: "SUP-1" };
			choices[3] = {
				action: "edit",
				values: { ...records[3].values, gst_hsn_code: "3304" },
				existing: "",
			};
			choices[6].action = "skip";
			const out = buildDecisions(
				{ card_sha256: "abc", record_count: 7, records, questions: questions() },
				choices,
				{
					"AR-1": { option: "No", note: "" },
					"AR-2": { option: "Purchase invoice", note: "" },
					"AR-3": { option: "", note: "All good" },
				}
			);
			expect(out).toEqual({
				records: {
					0: { action: "use_existing", existing: "SUP-1" },
					1: { action: "create" },
					2: { action: "create" },
					3: { action: "edit", values: { gst_hsn_code: "3304" } },
					4: { action: "create" },
					5: { action: "apply" },
					6: { action: "skip" },
				},
				answers: {
					"AR-1": { text: "No", approve: 1 },
					"AR-2": { text: "Purchase invoice", approve: 1 },
					"AR-3": { text: "All good", approve: 1 },
				},
				expected_card_sha256: "abc",
				record_count: 7,
			});
		});

		it("sends a cascaded record's own choice (the server skips it)", () => {
			const records = sheet();
			const choices = initialChoices(records);
			choices[0].action = "skip";
			choices[1] = {
				action: "edit",
				values: { ...records[1].values, city: "Pune" },
				existing: "",
			};
			const out = buildDecisions(
				{ card_sha256: "s", record_count: 7, records },
				choices,
				{}
			);
			expect([out.records[0], out.records[1], out.records[2]]).toEqual([
				{ action: "skip" },
				{ action: "edit", values: { city: "Pune" } },
				{ action: "create" },
			]);
		});

		it("sends every record skipped when the file is skipped", () => {
			const records = sheet();
			const choices = initialChoices(records);
			choices[0] = { action: "use_existing", values: null, existing: "SUP-1" };
			const out = buildDecisions(
				{ card_sha256: "s", record_count: 7, records, questions: questions() },
				choices,
				{ "AR-2": { option: "Skip this file", note: "" } }
			);
			expect(Object.values(out.records)).toEqual(records.map(() => ({ action: "skip" })));
			expect(out.answers["AR-2"]).toEqual({ text: "Skip this file", approve: 1 });
		});

		it("C-2: a reject-type answer on a non-routing Approve/Reject question sends approve 0", () => {
			const pair = [
				{
					name: "AR-5",
					question: "OK to bill?",
					options: ["Approve", "Reject"],
					routing: "",
					status: "Pending",
				},
			];
			const sheetOf = (questions) => ({
				card_sha256: "s",
				record_count: 0,
				records: [],
				questions,
			});
			const rejected = buildDecisions(
				sheetOf(pair),
				{},
				{
					"AR-5": { option: "Reject", note: "too soon" },
				}
			);
			expect(rejected.answers["AR-5"]).toEqual({ text: "Reject: too soon", approve: 0 });
			const approved = buildDecisions(
				sheetOf(pair),
				{},
				{
					"AR-5": { option: "Approve", note: "" },
				}
			);
			expect(approved.answers["AR-5"]).toEqual({ text: "Approve", approve: 1 });
			// a routing question's Approve/Reject-shaped options are always answered, never rejected
			const routing = [{ ...pair[0], name: "AR-6", routing: "skill_conflict" }];
			const routed = buildDecisions(
				sheetOf(routing),
				{},
				{
					"AR-6": { option: "Reject", note: "" },
				}
			);
			expect(routed.answers["AR-6"]).toEqual({ text: "Reject", approve: 1 });
			// a non-pair set of options (Yes/No) never triggers reject semantics
			const yesNo = buildDecisions(
				sheetOf(questions()),
				{},
				{
					"AR-1": { option: "No", note: "" },
					"AR-2": { option: "Skip this file", note: "" },
					"AR-3": { option: "", note: "fine" },
				}
			);
			expect(yesNo.answers["AR-1"]).toEqual({ text: "No", approve: 1 });
		});
	});

	describe("set for all", () => {
		const metas = {
			Item: { fields: [{ fieldname: "item_name", label: "Item Name" }, HSN_FIELD] },
		};

		it("offers a field two or more records of a section need, missing or named by a fix", () => {
			const records = sheet().filter((r) => r.category === "item");
			const [g] = setForAllGroups(records, initialChoices(sheet()), new Map(), metas);
			expect(g).toMatchObject({
				fieldname: "gst_hsn_code",
				label: "HSN/SAC",
				indexes: [3, 4],
			});
			expect(g.field.fieldtype).toBe("Link");
		});

		it("leaves out a skipped, existing, cascaded or already filled record", () => {
			const records = sheet().filter((r) => r.category === "item");
			const choices = initialChoices(sheet());
			choices[4].action = "skip";
			expect(setForAllGroups(records, choices, new Map(), metas)).toEqual([]);
			choices[4].action = "create";
			choices[3] = { action: "edit", values: { gst_hsn_code: "1" }, existing: "" };
			expect(setForAllGroups(records, choices, new Map(), metas)).toEqual([]);
			choices[3].action = "create";
			expect(setForAllGroups(records, choices, new Map([[4, 0]]), metas)).toEqual([]);
		});

		it("takes a fix's field by name; only a fix without one names it in words", () => {
			const items = () => sheet().filter((r) => r.category === "item");
			const records = items();
			records[0].needs_input = [];
			records[0].needs_fix = { ...HSN_FLAG, message: "Item Name is odd" };
			const [g] = setForAllGroups(records, initialChoices(sheet()), new Map(), metas);
			expect(g).toMatchObject({ fieldname: "gst_hsn_code", indexes: [3, 4] });
			const legacy = items();
			legacy[0].needs_input = [];
			legacy[0].needs_fix = HSN_FIX;
			legacy[1].needs_fix = { field: null, label: null, message: HSN_FIX };
			expect(
				setForAllGroups(legacy, initialChoices(sheet()), new Map(), metas)[0].indexes
			).toEqual([3, 4]);
			// a field the doctype's main fields lack gets no offer
			records[0].needs_fix = { ...HSN_FLAG, field: "rate" };
			expect(setForAllGroups(records, initialChoices(sheet()), new Map(), metas)).toEqual(
				[]
			);
		});

		it("needs the doctype's fields to read a fix; a lone record gets no offer", () => {
			const records = sheet().filter((r) => r.category === "item");
			expect(setForAllGroups(records, initialChoices(sheet()), new Map(), {})).toEqual([]);
		});

		it("names the longest field label a fix mentions, on word edges", () => {
			const fields = [
				{ fieldname: "item_name", label: "Item Name", fieldtype: "Data" },
				{ fieldname: "item", label: "Item", fieldtype: "Data" },
				HSN_FIELD,
				{ fieldname: "items", label: "Items", fieldtype: "Table" },
			];
			expect(fieldNamedIn(HSN_FIX, fields).fieldname).toBe("gst_hsn_code");
			expect(fieldNamedIn("Item Name is too long", fields).fieldname).toBe("item_name");
			expect(fieldNamedIn("Itemised totals", fields)).toBeNull();
			expect(fieldNamedIn("", fields)).toBeNull();
		});
	});

	it("says why a cascaded record is skipped, naming what it needs directly", () => {
		const records = sheet();
		const choices = initialChoices(records);
		choices[0].action = "skip";
		const skipped = cascade(records, choices);
		expect(cascadeReason(records[1], choices, skipped)).toBe(
			"Also skipped: it needs #1, which you're skipping."
		);
		expect(cascadeReason(records[2], choices, skipped)).toBe(
			"Also skipped: it needs #2, which is skipped with #1."
		);
		expect(cascadeReason(records[3], choices, skipped)).toBe("");
		expect(cascadeNote(1)).toBe("Applying also skips 1 record that needs a skipped one.");
		expect(cascadeNote(2)).toBe("Applying also skips 2 records that need a skipped one.");
	});

	it("splits server errors onto records, questions and the sheet", () => {
		expect(
			splitErrors(
				{
					3: "Blocked by #1: it needs that record.",
					"AR-1": "Answer this question.",
					sheet: "x",
				},
				questions()
			)
		).toEqual({
			records: { 3: "Blocked by #1: it needs that record." },
			questions: { "AR-1": "Answer this question." },
			sheet: ["x"],
		});
		expect(splitErrors(null, [])).toEqual({ records: {}, questions: {}, sheet: [] });
	});

	it("words every refusal", () => {
		const said = (reason_code, message) =>
			sheetRefusal({ ok: false, reason_code, error: message ? { message } : undefined });
		expect(said("changed")).toBe("This sheet changed since you opened it. Reloading it…");
		expect(said("busy")).toBe(
			"Someone is acting on this sheet right now. Try again in a moment."
		);
		expect(said("executing")).toBe("This sheet is being applied right now.");
		expect(said("already_handled")).toBe("This sheet was already handled.");
		expect(said("not_found")).toBe("This sheet is no longer available.");
		expect(said("unavailable")).toBe(
			"The apply couldn't start. Nothing was created. Try again."
		);
		expect(said("identity_refused")).toBe(
			"This can no longer run as the person who dropped the file. Skip this file, or ask them to drop it again."
		);
		expect(said("armed_run")).toBe("This run is stopping, so nothing was applied.");
		expect(said("collecting")).toBe(
			"Jarvis is still listing this sheet. Apply it once the run pauses."
		);
		expect(said("invalid", "decisions must be a JSON object.")).toBe(
			"Some records or answers need attention. Nothing was applied."
		);
		expect(said("tampered")).toBe(
			"This sheet failed an integrity check. Nothing was created."
		);
		expect(said("weird", "Server words")).toBe("Server words");
		expect(said("weird")).toBe("This sheet could not be applied.");
	});

	it("knows when a refusal means the sheet is gone", () => {
		expect(isSheetSettled({ ok: false, reason_code: "already_handled" })).toBe(true);
		expect(isSheetSettled({ ok: false, reason_code: "tampered", pa_status: "Failed" })).toBe(
			true
		);
		expect(isSheetSettled({ ok: false, reason_code: "busy" })).toBe(false);
		expect(
			isSheetSettled({ ok: false, reason_code: "executing", pa_status: "Executing" })
		).toBe(false);
	});

	it("sums up an outcome", () => {
		const s = outcomeSummary({
			records: [
				{ index: 0, doctype: "Supplier", title: "Acme", action: "created", name: "SUP-1" },
				{
					index: 1,
					doctype: "Item",
					title: "Rouge",
					action: "existing",
					name: "IT-1",
					auto: 1,
				},
				{ index: 2, doctype: "Item", title: "Kohl", action: "updated", name: "IT-2" },
				{ index: 3, doctype: "Address", title: "Acme-B", action: "skipped", cascade: 1 },
			],
			answers: [{ name: "AR-1", title: "Freight", answer: "Yes" }],
		});
		expect(s.line).toBe("Created 1 · used 1 existing · updated 1 · skipped 1");
		expect(s.rows.map((r) => r.text)).toEqual([
			"Created Supplier Acme as SUP-1",
			"Used the existing Item IT-1 for Rouge (it already existed)",
			"Updated Item IT-2",
			"Skipped Address Acme-B (it needed a skipped record)",
		]);
		expect(s.answers).toEqual([{ key: "AR-1", question: "Freight", answer: "Yes" }]);
		expect(outcomeSummary(null)).toEqual({ line: "", rows: [], answers: [] });
	});

	it("words a used-existing or skipped record without the auto/cascade aside", () => {
		const s = outcomeSummary({
			records: [
				{
					index: 0,
					doctype: "Item",
					title: "Rouge",
					action: "existing",
					name: "IT-1",
				},
				{ index: 1, doctype: "Address", title: "Acme-B", action: "skipped" },
			],
		});
		expect(s.rows.map((r) => r.text)).toEqual([
			"Used the existing Item IT-1 for Rouge",
			"Skipped Address Acme-B",
		]);
	});

	it("words progress and the read-only states", () => {
		expect(progressText({ done: 40, total: 250 })).toBe("Applying the sheet… 40 of 250");
		expect(progressText(null)).toBe("Applying the sheet…");
		expect(sheetStatusLine({ status: "Executed" })).toBe("Applied.");
		expect(sheetStatusLine({ status: "Discarded", reason_code: "discarded" })).toBe(
			"Skipped. Nothing from this sheet was created."
		);
		expect(
			sheetStatusLine({ status: "Discarded", reason_code: "cancelled", reason: "Stopped." })
		).toBe("Stopped.");
		expect(sheetStatusLine({ status: "Failed", reason: "It got stuck." })).toBe(
			"It got stuck."
		);
		expect(sheetStatusLine({ status: "Failed" })).toBe("This sheet could not be applied.");
		expect(sheetStatusLine({ status: "Pending", can_act: 0 })).toBe(
			"This sheet can't be verified right now, so it can't be applied. Try again, or skip this file."
		);
	});

	// FE-1: the copy is routed through __(), so a page translator (window.__) is honored.
	it("routes copy through a page translator when one is present", () => {
		window.__ = (text, args = []) =>
			"[t] " +
			String(text).replace(/\{(\d+)\}/g, (m, i) => (args[i] != null ? String(args[i]) : m));
		try {
			expect(progressText({ done: 1, total: 2 })).toBe("[t] Applying the sheet… 1 of 2");
			expect(sheetStatusLine({ status: "Executed" })).toBe("[t] Applied.");
			expect(cascadeNote(1)).toBe(
				"[t] Applying also skips 1 record that needs a skipped one."
			);
			expect(sheetTitle(null)).toBe("[t] Approval sheet");
		} finally {
			delete window.__;
		}
	});
});
