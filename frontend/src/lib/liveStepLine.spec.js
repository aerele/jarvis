import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * The live steps box: while a turn runs, the model's own "what I'm doing"
 * sentences (the bench's run:step events) are recorded for the whole turn
 * instead of a single replaced line, and narration that might still become a
 * step types into the current step rather than the reply (T5a, plan
 * .claude/workflow/plans/2026-09-28-live-turn-steps.md). T5b mounted the
 * StepsBox component off this state (its own rendering is pinned in
 * StepsBox.spec.js) and removed the old single-line markup (jv-livestep) and
 * the five other live blocks entirely, so these pin that the state/wiring
 * survived the re-mount: the list cannot outlive its run (or, for a
 * recovering row, its message), and ChatView wires @/lib/liveTurn's pure
 * functions the way the design intends.
 *
 * Source-fenced, the same way typedConfirm.spec.js pins ChatView's wiring.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

function caseBody(name) {
	const at = src.indexOf(`case "${name}":`);
	expect(at).toBeGreaterThan(-1);
	const next = src.indexOf("\n\t\tcase ", at + 1);
	return src.slice(at, next);
}

describe("live steps box wiring", () => {
	it("imports the pure step/narration helpers from @/lib/liveTurn", () => {
		// Format-independent: prettier wraps the import once it outgrows a line.
		const block = src.match(/import \{([^}]*)\} from "@\/lib\/liveTurn";/);
		expect(block).not.toBeNull();
		const names = block[1]
			.split(",")
			.map((n) => n.trim())
			.filter(Boolean);
		for (const name of [
			"addStep",
			"foldedHead",
			"liveBox",
			"splitNarration",
			"stepsFromTexts",
			"turnToolNames",
		])
			expect(names).toContain(name);
	});

	it("the step list is tied to the run that sent it, so every run teardown hides it", () => {
		// Each teardown resets currentRunId (run:end, run:error, stop, switch);
		// binding the list to it means none of them has to clear it by hand.
		expect(src).toContain("liveSteps.value.runId === currentRunId.value");
		expect(src).toContain("return liveSteps.value.steps;");
		// The T5a back-compat single-line computed is gone (T5b mounted StepsBox).
		expect(src).not.toContain("const liveStep = computed(");
	});

	it("T5b: a recovering row keeps its own steps by msgId even though currentRunId is null (run:recovering clears it)", () => {
		const body = caseBody("run:step");
		expect(body).toContain("msgId: currentMsgId.value,");
		const stepsForFn = src.slice(
			src.indexOf("function stepsFor(m) {"),
			src.indexOf("function stepsFor(m) {") + 400
		);
		expect(stepsForFn).toContain(
			"if (m && liveSteps.value.msgId === m.name) return liveSteps.value.steps;"
		);
		// overrideFor's recovering branch is keyed off message_id too, not
		// currentRunId/currentMsgId — the two stay in lockstep.
		const overrideFn = src.slice(
			src.indexOf("function overrideFor(m) {"),
			src.indexOf("function overrideFor(m) {") + 400
		);
		expect(overrideFn).toContain("recovering.value.message_id === m.name");
	});

	it("run:step is fenced like tool events and grows the list with addStep, keyed to the artifact slot", () => {
		const body = caseBody("run:step");
		expect(body).toContain("pumpFenceReject(p)");
		expect(body).toContain("toolEventIsStale(p)");
		expect(body).toContain("const sameRun = liveSteps.value.runId === rid;");
		expect(body).toContain(
			"const slot = artifactKind.value ? artifactTickIndex.value : null;"
		);
		expect(body).toMatch(
			/steps: addStep\(\s*sameRun \? liveSteps\.value\.steps : \[\],\s*p\.text,\s*slot,\s*nextActivityOrd\(\)\s*\)/
		);
	});

	it("assistant:delta splits narration and paces only the answer into the reply", () => {
		const body = caseBody("assistant:delta");
		expect(body).toContain('splitNarration(p.text || "")');
		expect(body).toContain("liveCandidate.value = candidateAfterDelta(liveCandidate.value, {");
		expect(body).toContain("runId: currentRunId.value,");
		expect(body).toContain('full: p.text || "",');
		// The reveal target is the ANSWER, not the raw text — a step candidate
		// must not flash into the reply and back out.
		expect(body).toContain("m.content = revealer.receive(p.message_id, answer);");
		// The old single-line clear-on-answer is gone: steps persist all turn.
		expect(body).not.toContain("liveStepState");
	});

	it("run:step takes over a sentence the relay's hide delta held on screen", () => {
		const body = caseBody("run:step");
		const steps = body.indexOf("steps: addStep(");
		const release = body.indexOf("if (liveCandidate.value.held)");
		expect(steps).toBeGreaterThan(-1);
		expect(release).toBeGreaterThan(steps); // same tick as the step: no empty frame
	});

	it("review C3: run:end, run:error and stopRun all snap a still-typing candidate before their own flush", () => {
		expect(src).toContain('import { candidateTextFor } from "@/lib/candidateSnap";');
		expect(src).toContain("function snapCandidateInto(messageId) {");
		const helper = src.slice(
			src.indexOf("function snapCandidateInto(messageId) {"),
			src.indexOf("function snapCandidateInto(messageId) {") + 400
		);
		expect(helper).toContain(
			"candidateTextFor(liveCandidate.value, messageId, currentRunId.value)"
		);
		expect(helper).toContain("revealer.receive(messageId, full);");
		expect(helper).toContain(
			'liveCandidate.value = { runId: null, msgId: null, text: "", full: "" };'
		);

		const runEnd = caseBody("run:end");
		const runEndSnapAt = runEnd.indexOf("snapCandidateInto(p.message_id);");
		const runEndFlushAt = runEnd.indexOf("flushReveal(p.message_id);");
		expect(runEndSnapAt).toBeGreaterThan(-1);
		expect(runEndFlushAt).toBeGreaterThan(runEndSnapAt);

		// run:error is the LAST case in the switch, so caseBody's "next case"
		// search would run off the end into the rest of the file — bound it
		// with a fixed window instead (same precedent as artifactActivityCard
		// .test.js's own run:error slice).
		const runErrorAt = src.indexOf('case "run:error":');
		expect(runErrorAt).toBeGreaterThan(-1);
		const runError = src.slice(runErrorAt, runErrorAt + 2000);
		const runErrorSnapAt = runError.indexOf("snapCandidateInto(p.message_id);");
		const runErrorFlushAt = runError.indexOf("flushReveal(p.message_id);");
		expect(runErrorSnapAt).toBeGreaterThan(-1);
		expect(runErrorFlushAt).toBeGreaterThan(runErrorSnapAt);

		const stopRunAt = src.indexOf("function stopRun() {");
		expect(stopRunAt).toBeGreaterThan(-1);
		const stopRun = src.slice(stopRunAt, src.indexOf("\n}", stopRunAt));
		const stopSnapAt = stopRun.indexOf("snapCandidateInto(m.name);");
		const stopFlushAt = stopRun.indexOf("flushReveal(m.name);");
		expect(stopSnapAt).toBeGreaterThan(-1);
		expect(stopFlushAt).toBeGreaterThan(stopSnapAt);
	});

	it("run:start upserts the assistant row, deduped by message id", () => {
		const body = caseBody("run:start");
		expect(body).toContain(
			"if (p.message_id && !messages.value.some((x) => x.name === p.message_id)) {"
		);
		expect(body).toContain(
			'{ name: p.message_id, role: "assistant", content: "", streaming: true }'
		);
	});

	it("a reload seeds the run/step/fence state from the server's live-turn contract", () => {
		expect(src).toContain(
			"liveSteps.value = {\n\t\t\t\t\trunId: _streaming.run_id,\n\t\t\t\t\tmsgId: _streaming.name,\n\t\t\t\t\tsteps: stepsFromTexts(_streaming.live_steps, _streaming.live_step_times),"
		);
		expect(src).toContain("currentRunId.value = _streaming.run_id;");
		expect(src).toContain("pumpFenceAccept(");
	});

	it("T5b: the old single-line markup is gone, replaced by one StepsBox per turn mounted from a memoised map", () => {
		expect(src).not.toContain("jv-livestep");
		expect(src).not.toMatch(/\(activeTools\.length \|\| waiting \|\| liveStep\)/);
		// boxViewByMsg is a computed MAP (one boxViewFor call per visible
		// assistant row), not a per-render template call.
		const mapFn = src.slice(
			src.indexOf("const boxViewByMsg = computed("),
			src.indexOf("const boxViewByMsg = computed(") + 300
		);
		expect(mapFn).toContain("Object.fromEntries(");
		expect(mapFn).toContain('.filter((m) => m.role === "assistant")');
		expect(mapFn).toContain("boxViewFor(m)");
		// Mounted in the reply row's #above-body, replacing the old jv-activity
		// strip; toggle/open state is the same activityOpen ChatView already kept.
		expect(src).toMatch(
			/<StepsBox\s+v-if="boxViewByMsg\[m\.name\]"\s+:view="boxViewByMsg\[m\.name\]"\s+:open="!!activityOpen\[m\.name\]"\s+@toggle="toggleActivity\(m\.name\)"/
		);
	});

	it("T5b: exactly one synthetic in-flight row (one JarvisMark + StepsBox), replacing the old queued chip", () => {
		expect(src).not.toContain("jv-queued-cancel");
		expect(src).toMatch(
			/<div v-if="inFlightRowVisible" style="display: flex; gap: 12px">[\s\S]{0,300}<JarvisMark[\s\S]{0,200}<StepsBox :view="inFlightView" @cancel="cancelQueued" \/>/
		);
	});

	it("review C4: boxViewFor routes a stopped/errored row to the settled branch unconditionally, before the isLive check decides anything else", () => {
		const at = src.indexOf("function boxViewFor(m) {");
		expect(at).toBeGreaterThan(-1);
		const body = src.slice(at, src.indexOf("\n}", at));
		const isLiveAt = body.indexOf("const isLive =");
		const guardAt = body.indexOf("if (!isLive || m.stopped || m.error) {");
		expect(isLiveAt).toBeGreaterThan(-1);
		expect(guardAt).toBeGreaterThan(isLiveAt);
		// The guard must be the FIRST `if` after isLive is computed — no other
		// branch decision sneaks in between.
		expect(body.indexOf("if (", isLiveAt)).toBe(guardAt);
		// One code path computes the stopped/failed head: the live-fold branch
		// (the run-clock head) must never receive `stopped`/`failed` — patching
		// its arguments instead of routing earlier was the rejected fix.
		const liveFoldAt = body.indexOf("seconds: runStartMs.value ?");
		expect(liveFoldAt).toBeGreaterThan(guardAt);
		const liveFoldCall = body.slice(liveFoldAt, liveFoldAt + 200);
		expect(liveFoldCall).not.toContain("stopped:");
		expect(liveFoldCall).not.toContain("failed:");
	});

	it("flow F5: a stop keeps the run's time and tools for the Stopped head, before it clears activeTools", () => {
		const at = src.indexOf("function stopRun() {");
		const body = src.slice(at, src.indexOf("\n}", at));
		const capture = body.indexOf("finishedRun.value = {");
		expect(capture).toBeGreaterThan(-1);
		expect(capture).toBeLessThan(body.indexOf("activeTools.value = [];"));
	});

	it("flow F6: a mid-turn reload splits the stored text like a live delta, so narration types into the box", () => {
		const at = src.indexOf("if (_streaming.run_id) {");
		const seed = src.slice(at, at + 2600);
		expect(seed).toContain('splitNarration(_streaming.content || "", { ended })');
		expect(seed).toContain("const ended = RUN_ENDED_STATES.has(_streaming.turn_state);");
		expect(src).toMatch(/const RUN_ENDED_STATES = new Set\(\[[^\]]*"done"[^\]]*\]\)/);
		expect(seed).toContain("_streaming.content = answer;");
		expect(seed).toMatch(/liveCandidate\.value = \{[\s\S]{0,120}msgId: _streaming\.name,/);
	});

	it("flow F1: a mid-turn reload starts the run clock from the reply row's creation", () => {
		const at = src.indexOf("if (_streaming.run_id) {");
		expect(at).toBeGreaterThan(-1);
		const seed = src.slice(at, at + 1400);
		expect(seed).toContain('Date.parse(String(_streaming.creation || "").replace(" ", "T"))');
		expect(seed).toContain("runStartMs.value = started;");
	});

	it("flow F2: the folded head counts saved rows plus tools seen live, and keeps the tab's own run time over the saved duration", () => {
		const end = caseBody("run:end");
		expect(end).toContain("finishedRun.value = {");
		expect(end.indexOf("finishedRun.value = {")).toBeLessThan(
			end.indexOf("snapCandidateInto(")
		);
		const at = src.indexOf("function boxViewFor(m) {");
		const body = src.slice(at, src.indexOf("\n}", at));
		expect(body).toContain("seconds: (fin && fin.seconds) || elapsedOf(m)");
		expect(body).toContain("toolNames: toolNamesFor(m, fin && fin.tools)");
		expect(body).toContain("toolNames: toolNamesFor(m, visibleActiveTools.value)");
		expect(src).toContain(
			"return turnToolNames(activityByAssistant.value[m.name] || [], liveTools);"
		);
	});

	it("the head's time never jumps at the finish: the live fold reads the run clock run:end stamps", () => {
		// e2e2 2026-09-29: the live fold froze at the moment the answer showed
		// ("Worked 30s") and jumped to the run span at run:end ("Worked 32s").
		const at = src.indexOf("function boxViewFor(m) {");
		const body = src.slice(at, src.indexOf("\n}", at));
		expect(body).toContain(
			"seconds: runStartMs.value ? (nowMs.value - runStartMs.value) / 1000 : null,"
		);
		expect(caseBody("run:end")).toContain("seconds: shownRunSeconds(),");
		// ...but only while that clock ticks: a recovered answer lands after busy
		// went false at the park, and must not read the frozen clock (review D1).
		expect(src).toContain("const now = busy.value && nowMs.value ? nowMs.value : Date.now();");
		expect(src).not.toContain("liveAnswerShownAt");
	});

	it("a first reply prepares its session: waking survives run:start and nothing else shows yet", () => {
		const status = caseBody("run:status");
		expect(status).toContain("sessionPrepRunId.value = p.run_id || null;");
		const at = src.indexOf("function overrideFor(m) {");
		const body = src.slice(at, src.indexOf("\n}", at));
		expect(body).toMatch(/isPreparingSession\(\{[\s\S]*sessionTurn: sessionTurnFor\(m\),/);
		expect(body).toContain("return SESSION_PREP;");
		// Pairing keeps its own copy and wins over it.
		expect(body.indexOf("preConnectStatusLabel(statusPhase.value)")).toBeLessThan(
			body.indexOf("return SESSION_PREP;")
		);
	});

	it("tool results merge live, so the count and Reports consulted never change at the reload", () => {
		const res = caseBody("tool:result");
		expect(res).toContain(
			"if (!currentRunId.value || !currentMsgId.value || !p.tool_message_id) break;"
		);
		expect(src).toContain(
			"const transcript = computed(() => withLiveToolRows(messages.value, liveToolRows.value));"
		);
		expect(src).toContain(
			"const reportToolsByTurn = computed(() => reportToolsByAssistant(transcript.value));"
		);
		const act = src.slice(src.indexOf("const activityByAssistant = computed(() => {"));
		expect(act.slice(0, 200)).toContain("for (const m of transcript.value) {");
		expect(caseBody("run:start")).toContain(
			"liveToolRows.value = { msgId: p.message_id || null, rows: [] };"
		);
		// Steps and results carry this tab's arrival order for the expanded head.
		expect(res).toContain("toolRowFromResult(p, nextActivityOrd())");
		// A confirm/discard receipt is never merged into the live reply (/code-review).
		expect(res).toContain("if (p.action_outcome) break;");
		// The expanded head lists saved (or live) steps with the tool rows.
		expect(src).toContain('v-for="item in activityDetailsFor(m)"');
		expect(src).toContain(
			"return activityItems(steps, activityByAssistant.value[m.name] || []);"
		);
		// Record names link as the answer lands, not after the reload.
		expect(src).toContain("const docRefs = computed(() => collectDocRefs(transcript.value));");
	});
});
