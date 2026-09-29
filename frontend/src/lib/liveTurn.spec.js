import { describe, it, expect } from "vitest";
import {
	MAX_CANDIDATE_CHARS,
	addStep,
	countLabel,
	foldedHead,
	formatWorked,
	isLookupTool,
	isStepCandidate,
	liveBox,
	splitNarration,
	stepSubline,
	stepsFromTexts,
	turnToolNames,
} from "./liveTurn.js";

// Real narration from e2e2 (2026-09-28, pooled ChatGPT, run 386a51ea823d).
const S1 =
	"I’ll find the invoice contact details, then prepare payment-reminder drafts for your review.";
const S2 =
	"The invoice records don’t contain email addresses, so I’m checking each customer’s linked contacts before I draft the reminders.";
const S3 =
	"I’m locating the linked Contact records so the reminders go only to valid customer email addresses.";
const S4 =
	"I’ve confirmed the invoices themselves have no email recipient. I’m checking whether the customers’ linked contacts have usable email addresses.";

const texts = (steps) => steps.map((s) => s.text);

describe("addStep", () => {
	it("appends a new sentence without mutating the list", () => {
		const a = addStep([], S1);
		const b = addStep(a, S2);
		expect(texts(b)).toEqual([S1, S2]);
		expect(texts(a)).toEqual([S1]);
	});

	it("ignores the runtime's repeats (preamble update, end, and the relay's move)", () => {
		let steps = addStep([], S1);
		steps = addStep(steps, S1);
		steps = addStep(steps, `  ${S1} `);
		steps = addStep(addStep(steps, S2), S1);
		expect(texts(steps)).toEqual([S1, S2]);
	});

	it("a growing preamble replaces the last step and keeps its slot; a shorter re-send is ignored", () => {
		let steps = addStep([], "I’m locating the linked Contact records", 2);
		steps = addStep(steps, S3, 3);
		expect(steps).toEqual([{ text: S3, slot: 2 }]);
		expect(addStep(steps, "I’m locating")).toEqual(steps);
	});

	it("ignores blank text and bad slots", () => {
		expect(texts(addStep(addStep([], S1), "  "))).toEqual([S1]);
		expect(addStep(null, "")).toEqual([]);
		expect(addStep([], S1, -1)).toEqual([{ text: S1, slot: null }]);
	});

	it("rebuilds from the server's live_steps after a reload", () => {
		expect(stepsFromTexts([S1, S1, S2])).toEqual([
			{ text: S1, slot: null },
			{ text: S2, slot: null },
		]);
		expect(stepsFromTexts(undefined)).toEqual([]);
	});
});

describe("narration candidate", () => {
	it("keeps lockstep with the backend's preamble limit (steps.py MAX_PREAMBLE_STEP_CHARS)", () => {
		expect(MAX_CANDIDATE_CHARS).toBe(320);
	});

	it("short one-line text types into the current step, including two sentences", () => {
		expect(isStepCandidate(S3)).toBe(true);
		expect(isStepCandidate(S4)).toBe(true);
		expect(splitNarration(S3)).toEqual({ candidate: S3, answer: "" });
	});

	it("a newline, a markdown block or too much text is the answer", () => {
		expect(isStepCandidate("You have 3 invoices.\n\nDetails")).toBe(false);
		expect(isStepCandidate("| Invoice | Amount |")).toBe(false);
		expect(isStepCandidate("- first item")).toBe(false);
		expect(isStepCandidate("1. first item")).toBe(false);
		expect(isStepCandidate("```jarvis-cards")).toBe(false);
		expect(isStepCandidate("x".repeat(MAX_CANDIDATE_CHARS + 1))).toBe(false);
		expect(splitNarration("Line one\nLine two")).toEqual({
			candidate: "",
			answer: "Line one\nLine two",
		});
	});

	it("at the end of the run the text is the answer, however short", () => {
		expect(splitNarration("There are 3 customers.", { ended: true })).toEqual({
			candidate: "",
			answer: "There are 3 customers.",
		});
	});

	it("the relay's empty mirror leaves nothing typing", () => {
		expect(splitNarration("")).toEqual({ candidate: "", answer: "" });
	});
});

describe("folded head", () => {
	it("says how long it worked and what it did, in plain words", () => {
		expect(
			foldedHead({ seconds: "48.2", toolNames: ["get_doc", "jarvis__get_list", "query"] })
		).toEqual({
			label: "Worked 48s",
			count: "3 lookups",
			finishing: false,
			subline: "",
			expandable: true,
			tone: "done",
		});
	});

	it("any write makes them actions", () => {
		expect(countLabel(["get_list", "send_email"])).toBe("2 actions");
		expect(countLabel(["get_doc"])).toBe("1 lookup");
		expect(countLabel([])).toBe("");
		expect(isLookupTool("jarvis__get_linked_docs")).toBe(true);
		expect(isLookupTool("read")).toBe(true);
		expect(isLookupTool("export_excel")).toBe(false);
		// Memory recall and a record preview only read (flow review F4: a turn
		// that recalled memory used to read "actions").
		expect(isLookupTool("recall")).toBe(true);
		expect(isLookupTool("jarvis__preview_doc")).toBe(true);
		expect(isLookupTool("create_doc")).toBe(false);
		expect(countLabel(["recall", "query", "get_schema"])).toBe("3 lookups");
	});

	it("stop and failure keep the same line", () => {
		const stopped = foldedHead({
			seconds: 21,
			toolNames: ["get_doc", "query"],
			stopped: true,
		});
		expect(stopped.label).toBe("Stopped after 21s");
		expect(stopped.subline).toBe("You stopped this reply.");
		expect(stopped.tone).toBe("stopped");
		const failed = foldedHead({ seconds: 185, toolNames: ["get_doc"], failed: true });
		expect(failed.label).toBe("Failed after 3m 5s");
		expect(failed.tone).toBe("failed");
		expect(foldedHead({ stopped: true }).label).toBe("Stopped");
	});

	it("finishing rides on the line until enrichment lands", () => {
		expect(
			foldedHead({ seconds: 52, toolNames: ["export_excel"], finishing: true }).finishing
		).toBe(true);
		expect(foldedHead({ finishing: true })).not.toBeNull();
	});

	it("activity detail off: time only, not expandable", () => {
		const h = foldedHead({ seconds: 52, toolNames: ["get_doc"], showDetail: false });
		expect(h.count).toBe("");
		expect(h.expandable).toBe(false);
		expect(h.label).toBe("Worked 52s");
	});

	it("an old reply with no duration and no tools gets no line", () => {
		expect(foldedHead({})).toBeNull();
	});

	it("formats whole seconds and minutes", () => {
		expect(formatWorked(0.4)).toBe("1s");
		expect(formatWorked(59.4)).toBe("59s");
		expect(formatWorked(240)).toBe("4m");
		expect(formatWorked("")).toBe("");
		expect(formatWorked(-3)).toBe("");
	});
});

describe("sub-line", () => {
	it("names the running tool, then the time", () => {
		expect(
			stepSubline({ toolPhrase: "Checking the Contact structure…", elapsed: "34s" })
		).toBe("Checking the Contact structure… · 34s");
	});

	it("says it is reading results between tools", () => {
		expect(stepSubline({ analyzing: true, elapsed: "21s" })).toBe("Reading results · 21s");
	});

	it("activity detail off shows the time only", () => {
		expect(
			stepSubline({ toolPhrase: "Opening Sales Invoice…", elapsed: "8s", showDetail: false })
		).toBe("8s");
	});
});

describe("live box, plain turns", () => {
	it("before any narration the current step is the idle word", () => {
		const box = liveBox({ waiting: true, idleText: "Thinking" });
		expect(box.rows).toEqual([{ text: "Thinking", state: "current", sub: "" }]);
		expect(box.header).toBeNull();
		expect(box.announce).toBe("Thinking");
	});

	it("done steps are muted and the newest is current, with one sub-line", () => {
		const box = liveBox({
			steps: stepsFromTexts([S1, S2, S3]),
			activeTools: [{ name: "jarvis__get_schema", status: "running" }],
			toolPhrase: "Checking the Contact structure…",
			elapsed: "34s",
		});
		expect(box.rows.map((r) => r.state)).toEqual(["done", "done", "current"]);
		expect(box.rows[2]).toEqual({
			text: S3,
			state: "current",
			sub: "Checking the Contact structure… · 34s",
		});
		expect(box.earlier).toBe(0);
		expect(box.announce).toBe(S3);
	});

	it("shows the last three and folds older ones into +N earlier", () => {
		const box = liveBox({ steps: stepsFromTexts([S1, S2, S3, S4]) });
		expect(box.earlier).toBe(1);
		expect(box.rows.map((r) => r.text)).toEqual([S2, S3, S4]);
	});

	it("narration typing appears as the current step, never twice", () => {
		const typing = liveBox({
			steps: stepsFromTexts([S1]),
			candidate: "I’m locating the linked",
		});
		expect(box(typing)).toEqual([S1, "I’m locating the linked"]);
		// The relay then records the same sentence as a step: still one row.
		expect(box(liveBox({ steps: stepsFromTexts([S1, S3]), candidate: S3 }))).toEqual([S1, S3]);
		expect(
			box(liveBox({ steps: stepsFromTexts([S1, S3]), candidate: "I’m locating" }))
		).toEqual([S1, S3]);
		// A partial the relay recorded early is completed by the typing text.
		expect(
			box(liveBox({ steps: stepsFromTexts([S1, "I’m locating the"]), candidate: S3 }))
		).toEqual([S1, S3]);
	});

	it("after the tools finish the current step reads results", () => {
		const b = liveBox({
			steps: stepsFromTexts([S1]),
			activeTools: [{ name: "get_doc", status: "completed" }],
			elapsed: "9s",
		});
		expect(b.rows[0].sub).toBe("Reading results · 9s");
	});

	it("recovery takes the current step and keeps what was done", () => {
		const b = liveBox({
			steps: stepsFromTexts([S1]),
			elapsed: "72s",
			override: {
				text: "Finishing in the background",
				sub: "Reconnecting. Your answer will appear here when it's ready.",
			},
		});
		expect(b.rows).toEqual([
			{ text: S1, state: "done" },
			{
				text: "Finishing in the background",
				state: "current",
				sub: "Reconnecting. Your answer will appear here when it's ready. · 72s",
			},
		]);
	});

	it("an override with no narration replaces the idle word", () => {
		const b = liveBox({
			waiting: true,
			override: { text: "Taking you to the Dashboards builder" },
		});
		expect(b.rows).toEqual([
			{ text: "Taking you to the Dashboards builder", state: "current", sub: "" },
		]);
	});
});

function box(view) {
	return view.rows.map((r) => r.text);
}

describe("live box, file turns", () => {
	const PULL = "I’ll pull the overdue sales invoices, then build the spreadsheet.";
	const LAYOUT = "I’m laying out one row per invoice with the amount still outstanding.";

	it("before any tool: understanding is current, the rest upcoming", () => {
		const b = liveBox({ artifactKind: "pdf", waiting: true });
		expect(b.header).toEqual({ kind: "pdf", title: "Creating your PDF" });
		expect(b.rows.map((r) => r.state)).toEqual([
			"current",
			"upcoming",
			"upcoming",
			"upcoming",
		]);
	});

	it("narration takes the place of the phase it arrived during and never moves", () => {
		// A spreadsheet turn, frame by frame, as ChatView would record it.
		let steps = addStep([], PULL, 0); // said while understanding
		const fetching = liveBox({
			artifactKind: "spreadsheet",
			steps,
			activeTools: [{ name: "jarvis__get_list", status: "running" }],
		});
		expect(fetching.rows.map((r) => [r.text, r.state])).toEqual([
			[PULL, "done"],
			["Fetching data", "current"],
			["Composing", "upcoming"],
			["Delivering", "upcoming"],
		]);

		const composing = liveBox({
			artifactKind: "spreadsheet",
			steps,
			activeTools: [{ name: "jarvis__get_list", status: "completed" }],
		});
		expect(composing.rows.map((r) => [r.text, r.state])).toEqual([
			[PULL, "done"],
			["Fetching data", "done"],
			["Composing", "current"],
			["Delivering", "upcoming"],
		]);

		steps = addStep(steps, LAYOUT, 2); // said while composing: the FileGen board
		const narrated = liveBox({
			artifactKind: "spreadsheet",
			steps,
			activeTools: [{ name: "jarvis__get_list", status: "completed" }],
		});
		expect(narrated.rows.map((r) => [r.text, r.state])).toEqual([
			[PULL, "done"],
			["Fetching data", "done"],
			[LAYOUT, "current"],
			["Delivering", "upcoming"],
		]);

		const writing = liveBox({
			artifactKind: "spreadsheet",
			steps,
			activeTools: [
				{ name: "jarvis__get_list", status: "completed" },
				{ name: "jarvis__export_excel", status: "running" },
			],
			toolPhrase: "Preparing your spreadsheet…",
			elapsed: "18s",
		});
		// Four rows, the current one last, so the first scrolls into "+1 earlier".
		expect(writing.earlier).toBe(1);
		expect(writing.rows.map((r) => [r.text, r.state])).toEqual([
			["Fetching data", "done"],
			[LAYOUT, "done"],
			["Delivering", "current"],
		]);
		expect(writing.rows[2].sub).toBe("Preparing your spreadsheet… · 18s");
	});

	it("narration typing on a file turn sits in the current phase's slot", () => {
		const b = liveBox({
			artifactKind: "spreadsheet",
			steps: addStep([], PULL, 0),
			candidate: LAYOUT,
			activeTools: [{ name: "jarvis__get_list", status: "completed" }],
		});
		expect(box(b)).toEqual([PULL, "Fetching data", LAYOUT, "Delivering"]);
	});

	it("restored steps with no slot fill from the top", () => {
		const b = liveBox({
			artifactKind: "spreadsheet",
			steps: stepsFromTexts([PULL]),
			activeTools: [{ name: "jarvis__get_list", status: "completed" }],
		});
		expect(box(b)).toEqual([PULL, "Fetching data", "Composing", "Delivering"]);
	});

	it("an override drops the upcoming phases and keeps what was done", () => {
		const b = liveBox({
			artifactKind: "image",
			activeTools: [{ name: "image_generate", status: "completed" }],
			steps: addStep([], "I’ll draw the chart as an image.", 0),
			override: { text: "Waiting for the image" },
		});
		expect(b.rows[b.rows.length - 1]).toMatchObject({
			text: "Waiting for the image",
			state: "current",
		});
		expect(b.rows.some((r) => r.state === "upcoming")).toBe(false);
	});
});

describe("turnToolNames", () => {
	it("counts a saved row and its live event once when the ids match", () => {
		expect(
			turnToolNames(
				[{ tool_name: "get_list", tool_call_id: "call_1" }],
				[
					{ id: "call_1", name: "jarvis__get_list" },
					{ id: "call_2", name: "jarvis__query" },
				]
			)
		).toEqual(["get_list", "jarvis__query"]);
	});

	it("a saved row with no id still consumes its live entry (review D1)", () => {
		expect(
			turnToolNames(
				[{ tool_name: "get_list", tool_call_id: null }],
				[{ id: "jarvis__get_list-0", name: "jarvis__get_list" }]
			)
		).toEqual(["get_list"]);
	});

	it("a live event with no real id is consumed by its saved row", () => {
		expect(
			turnToolNames(
				[{ tool_name: "query", tool_call_id: "call_9" }],
				[{ id: "jarvis__query-3", name: "jarvis__query" }]
			)
		).toEqual(["query"]);
	});

	it("a live entry another saved row claims by id is not taken by name", () => {
		expect(
			turnToolNames(
				[
					{ tool_name: "get_doc", tool_call_id: null },
					{ tool_name: "get_doc", tool_call_id: "call_b" },
				],
				[
					{ id: "call_b", name: "jarvis__get_doc" },
					{ id: "call_c", name: "jarvis__get_doc" },
				]
			)
		).toEqual(["get_doc", "get_doc"]);
	});

	it("live tools with no saved row yet are added", () => {
		expect(turnToolNames([], [{ id: "x", name: "jarvis__get_schema" }])).toEqual([
			"jarvis__get_schema",
		]);
		expect(turnToolNames(undefined, undefined)).toEqual([]);
	});
});
