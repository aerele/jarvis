import { describe, it, expect } from "vitest";
import {
	MAX_CANDIDATE_CHARS,
	SESSION_PREP,
	activityItems,
	addStep,
	candidateAfterDelta,
	countLabel,
	foldedHead,
	formatWorked,
	hasSavedModelTools,
	isPreparingSession,
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
		// With the server's times, a restored step keeps when it was seen.
		expect(stepsFromTexts([S1, S2], ["2026-09-30 00:17:24.000000", null])).toEqual([
			{ text: S1, slot: null, at: "2026-09-30 00:17:24.000000" },
			{ text: S2, slot: null },
		]);
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

	it("types without markdown marks, the way the server shows the step", () => {
		// e2e2 2026-09-29: an answer's first line typed into the box as
		// "…for **Jarvis Agebt (Demo)**:" with the raw marks showing.
		const line =
			"Top customers as of 29-09-2026 for **Jarvis Agebt (Demo)**: `ACC-0001` __now__";
		expect(splitNarration(line)).toEqual({
			candidate: "Top customers as of 29-09-2026 for Jarvis Agebt (Demo): ACC-0001 now",
			answer: "",
		});
		// The answer keeps its marks: only the box text is shaped.
		expect(splitNarration(line, { ended: true }).answer).toBe(line);
	});
});

describe("arrival order and the expanded head", () => {
	it("a new step keeps the order it arrived in; a growing resend keeps its first", () => {
		let steps = addStep([], "Checking", null, 1);
		steps = addStep(steps, "Checking the ledger.", null, 4);
		expect(steps).toEqual([{ text: "Checking the ledger.", slot: null, ord: 1 }]);
		expect(addStep([], "No order")).toEqual([{ text: "No order", slot: null }]);
	});

	it("saved steps sit before the first tool row that came after them", () => {
		const steps = [
			{ text: "Checking the receivables register.", at: "2026-09-29 22:13:14.100000" },
			{ text: "Running the summary.", at: "2026-09-29 22:13:29.000000" },
		];
		const rows = [
			{ name: "t1", creation: "2026-09-29 22:13:15.278432" },
			{ name: "t2", creation: "2026-09-29 22:13:23.039016" },
			{ name: "t3", creation: "2026-09-29 22:13:30" },
		];
		expect(activityItems(steps, rows).map((i) => i.key)).toEqual([
			"step-0",
			"t1",
			"t2",
			"step-1",
			"t3",
		]);
	});

	it("live steps and live tool rows order by the arrival counter", () => {
		const items = activityItems(
			[
				{ text: "A", ord: 2 },
				{ text: "B", ord: 5 },
			],
			[
				{ name: "recall", ord: 1 },
				{ name: "query", ord: 3 },
			]
		);
		expect(items.map((i) => i.key)).toEqual(["recall", "step-0", "query", "step-1"]);
		expect(items[1]).toEqual({ kind: "step", key: "step-0", text: "A" });
		expect(items[0].kind).toBe("tool");
	});

	it("after a tab-focus resync, what the reload loaded sorts before what arrived live", () => {
		// e2e2 2026-09-30 (run 8): the resync saved the first-turn recall row
		// (server time) before the step and the query arrived live (counter);
		// the reload later showed recall, step, query - the live view must too.
		const items = activityItems(
			[{ text: "I’m checking your customer count.", slot: null, ord: 3 }],
			[
				{ name: "recall", creation: "2026-09-30 00:17:23.101000" },
				{ name: "query", ord: 4 },
			]
		);
		expect(items.map((i) => i.key)).toEqual(["recall", "step-0", "query"]);
		// A step restored by the resync carries its server time the same way.
		const restored = activityItems(
			[
				{ text: "A", at: "2026-09-30 00:17:24.000000" },
				{ text: "B", ord: 7 },
			],
			[
				{ name: "t1", creation: "2026-09-30 00:17:25.000000" },
				{ name: "t2", ord: 8 },
			]
		);
		expect(restored.map((i) => i.key)).toEqual(["step-0", "t1", "step-1", "t2"]);
	});

	it("an item with no key at all puts every step first", () => {
		const items = activityItems(
			[{ text: "A" }, { text: "B", ord: 9 }],
			[{ name: "t1", creation: "2026-09-29 22:13:15" }]
		);
		expect(items.map((i) => i.key)).toEqual(["step-0", "step-1", "t1"]);
		expect(activityItems([], []).length).toBe(0);
		expect(activityItems(null, [{ name: "t1" }]).map((i) => i.key)).toEqual(["t1"]);
	});
});

describe("narration held through the relay's hide", () => {
	const typing = { runId: "r1", msgId: "m1", text: S3, full: S3, held: false };
	const split = (full, over = {}) => ({
		runId: "r1",
		msgId: "m1",
		...splitNarration(full),
		...over,
		full,
		text: over.text ?? splitNarration(full).candidate,
	});

	it("a delta that empties the reply keeps the sentence typing, with no answer to snap", () => {
		// e2e2 2026-09-29: the hide delta landed a frame before run:step and the
		// box fell back to "Preparing your session" for that frame.
		expect(candidateAfterDelta(typing, split(""))).toEqual({
			runId: "r1",
			msgId: "m1",
			text: S3,
			full: "",
			held: true,
		});
	});

	it("new text, an answer or another run replaces it as before", () => {
		expect(candidateAfterDelta(typing, split("Checking"))).toMatchObject({
			text: "Checking",
			held: false,
		});
		const answer = "You have 3 customers.\n\nDetails";
		expect(candidateAfterDelta(typing, split(answer))).toEqual({
			runId: "r1",
			msgId: "m1",
			text: "",
			full: answer,
			held: false,
		});
		expect(candidateAfterDelta(typing, split("", { runId: "r2" }))).toMatchObject({
			text: "",
			held: false,
		});
		expect(candidateAfterDelta(null, split(""))).toMatchObject({ text: "", held: false });
	});
});

describe("preparing the session", () => {
	const quiet = { steps: [], candidate: "", activeTools: [], answer: "" };

	it("a session turn with nothing on screen yet is preparing", () => {
		expect(isPreparingSession({ ...quiet, sessionTurn: true })).toBe(true);
	});

	it("any sign of the turn ends it: a step, narration typing, a tool, answer text", () => {
		const on = { ...quiet, sessionTurn: true };
		expect(isPreparingSession({ ...on, steps: [{ text: S1, slot: null }] })).toBe(false);
		expect(isPreparingSession({ ...on, candidate: "Checking" })).toBe(false);
		expect(
			isPreparingSession({ ...on, activeTools: [{ name: "query", status: "running" }] })
		).toBe(false);
		expect(isPreparingSession({ ...on, answer: "You have" })).toBe(false);
		// A reload mid-turn: the tools this tab never saw start are saved rows.
		expect(isPreparingSession({ ...on, savedTools: true })).toBe(false);
		expect(isPreparingSession({ ...on, candidate: "   ", answer: "\n" })).toBe(true);
	});

	it("a reload mid-turn ends it only for tools the model ran, not the session's recall", () => {
		// A tab-focus resync reloads the transcript; the plugin's recall row is
		// saved ~0.5 s into every first turn with no call id.
		const recall = { name: "t0", role: "tool", tool_name: "recall", tool_call_id: null };
		const query = { name: "t1", role: "tool", tool_name: "query", tool_call_id: "call_1" };
		const rows = [{ name: "u1", role: "user" }, { name: "a1", role: "assistant" }, recall];
		expect(hasSavedModelTools(rows, "a1")).toBe(false);
		expect(hasSavedModelTools([...rows, query], "a1")).toBe(true);
		// A recall the model called itself carries a call id: that is work.
		expect(
			hasSavedModelTools([...rows.slice(0, 2), { ...recall, tool_call_id: "c" }], "a1")
		).toBe(true);
		// Tool rows of a later turn never count for this reply.
		expect(
			hasSavedModelTools([...rows.slice(0, 2), { name: "u2", role: "user" }, query], "a1")
		).toBe(false);
		expect(hasSavedModelTools(rows, "gone")).toBe(false);
	});

	it("a later turn of an ongoing session is never preparing", () => {
		expect(isPreparingSession({ ...quiet, sessionTurn: false })).toBe(false);
		expect(isPreparingSession()).toBe(false);
	});

	it("shows as the current step with its reason, keeping the elapsed time", () => {
		const box = liveBox({ override: SESSION_PREP, elapsed: "8s", idleText: "Thinking" });
		expect(box.rows).toEqual([
			{
				text: "Preparing your session",
				state: "current",
				sub: "The first reply takes a little longer · 8s",
			},
		]);
		expect(box.announce).toBe("Preparing your session");
	});
});

describe("folded head", () => {
	it("says how long it worked and what it did, in plain words", () => {
		expect(
			foldedHead({ seconds: "48.2", toolNames: ["get_doc", "jarvis__get_list", "query"] })
		).toEqual({
			label: "Worked 48s",
			count: "3 tools",
			finishing: false,
			subline: "",
			expandable: true,
			tone: "done",
		});
	});

	it("counts every tool the turn ran, reads and writes alike", () => {
		expect(countLabel(["get_list", "send_email"])).toBe("2 tools");
		expect(countLabel(["get_doc"])).toBe("1 tool");
		expect(countLabel(["recall", "query", "get_schema"])).toBe("3 tools");
		expect(countLabel([])).toBe("");
		expect(countLabel(undefined)).toBe("");
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

	it("a settled turn that ran no tools has no bar; a running or tooled one keeps it", () => {
		expect(foldedHead({ seconds: 3, toolNames: [], settled: true })).toBeNull();
		expect(foldedHead({ seconds: 3, toolNames: [] }).label).toBe("Worked 3s");
		expect(foldedHead({ seconds: 3, toolNames: ["get_doc"], settled: true }).expandable).toBe(
			true
		);
		expect(foldedHead({ seconds: 3, stopped: true, settled: true }).label).toBe(
			"Stopped after 3s"
		);
		expect(foldedHead({ seconds: 3, failed: true, settled: true }).tone).toBe("failed");
		expect(foldedHead({ seconds: 3, finishing: true, settled: true }).finishing).toBe(true);
	});

	it("a saved reply whose tool strip is empty keeps its bar unless the caller knows nothing ran", () => {
		// ChatView passes settled only when no tool row of any kind and no steps exist.
		expect(foldedHead({ seconds: 53, toolNames: [], settled: false })).not.toBeNull();
		expect(foldedHead({ seconds: 53, toolNames: [], settled: true })).toBeNull();
	});

	it("a streaming answer with no tool so far shows no bar; one appears when a tool has run", () => {
		const base = { seconds: 2, showDetail: true, settled: true };
		expect(foldedHead({ ...base, toolNames: [] })).toBeNull();
		expect(foldedHead({ ...base, toolNames: ["get_doc"] }).count).toBe("1 tool");
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
