// The steps box: one Jarvis mark and one box per assistant turn (design canvas
// https://claude.ai/artifact/6HhWob6TyVSRAPKoe22WFY, plan
// .claude/workflow/plans/2026-09-28-live-turn-steps.md).
//
// A running turn used to render up to six separate live blocks (queued chip,
// goto line, artifact card, generic block, recovering, compacting), each with
// its own animated mark, next to the reply row's own mark, and told progress
// four ways. This module turns the turn state ChatView already keeps into ONE
// view for StepsBox.vue:
//
//   live   - the step list: done steps muted, the current step with one
//            sub-line, upcoming artifact phases as hollow rings
//   folded - one line once the answer is showing: "Worked 48s · 5 tools"
//   queued - the only line of a turn waiting for a slot
//
// Pure functions only, no Vue, so every rule is pinned by liveTurn.spec.js.

import { toolBaseName } from "./dashboardBuildCard.js";
import { ARTIFACT_TITLES, artifactBuildPhase, artifactPhaseList } from "./artifactActivityCard.js";

// The longest reply text that can still be narration. Keep in lockstep with
// MAX_PREAMBLE_STEP_CHARS in jarvis/chat/steps.py: the backend moves a
// runtime-flagged preamble up to this length into the step list, so the
// client must treat text up to the same length as a possible step.
export const MAX_CANDIDATE_CHARS = 320;

// Done steps shown above the current one; older ones fold into "+N earlier".
export const VISIBLE_DONE_STEPS = 2;

const BLOCK_START = /^(?:\||#{1,6}\s|>|```|[-*+]\s|\d+[.)]\s)/;
// Bold, underline-bold and code marks. The server drops the same marks from a
// step (jarvis/chat/steps.py display_line), so typing narration matches the
// step it becomes and an answer's first line never shows raw "**" in the box.
const INLINE_MARK = /\*\*|__|`/g;

// The current step of a turn that is setting up its agent session.
export const SESSION_PREP = {
	text: "Preparing your session",
	sub: "The first reply takes a little longer",
};

/**
 * Record a step sentence, returning a NEW list (Vue reactivity).
 *
 * The runtime can send the same sentence more than once (a preamble update,
 * its end, and the relay's own move at the tool call), and a preamble can grow
 * while the model is still writing it. So: an exact repeat anywhere is
 * ignored, a longer version of the last step replaces it (keeping its slot),
 * and a shorter re-send of the last step is ignored.
 *
 * `slot` is the artifact phase that was current when the sentence arrived, so
 * on a file turn the sentence takes that phase's place and never moves as the
 * phases advance. Plain turns ignore it. `ord` is the order this tab saw it in
 * among the turn's steps and tool results (activityItems); a growing resend
 * keeps the first one.
 *
 * @param {Array<{text: string, slot: number|null, ord?: number}>} steps
 * @param {string} text
 * @param {number|null} [slot]
 * @param {number} [ord]
 * @returns {Array<{text: string, slot: number|null, ord?: number}>}
 */
export function addStep(steps, text, slot = null, ord = undefined) {
	const list = Array.isArray(steps) ? steps : [];
	const t = String(text || "").trim();
	if (!t || list.some((s) => s.text === t)) return list;
	const last = list[list.length - 1];
	if (last && t.startsWith(last.text)) return [...list.slice(0, -1), { ...last, text: t }];
	if (last && last.text.startsWith(t)) return list;
	const step = { text: t, slot: Number.isInteger(slot) && slot >= 0 ? slot : null };
	if (Number.isFinite(ord)) step.ord = ord;
	return [...list, step];
}

/**
 * Steps restored from the server after a reload: no slot, and the server time
 * each was seen at when it sent one (`times`, parallel to `texts`).
 */
export function stepsFromTexts(texts, times = []) {
	return (Array.isArray(texts) ? texts : []).reduce((list, t, i) => {
		const next = addStep(list, t);
		const at = Array.isArray(times) ? times[i] : null;
		if (next.length > list.length && typeof at === "string" && at)
			next[next.length - 1].at = at;
		return next;
	}, []);
}

// Place narration on a file turn: each sentence in the slot of the phase it
// arrived during (the next free slot if that one is taken, the next position
// when it has none), phases fill the rest.
function placeNarration(narration, phaseCount) {
	const slots = [];
	let next = 0;
	for (const step of narration) {
		let at = step.slot != null ? Math.max(step.slot, next) : next;
		while (slots[at] !== undefined) at++;
		slots[at] = step.text;
		next = at + 1;
	}
	return { slots, lastAt: next - 1, length: Math.max(phaseCount, slots.length) };
}

/**
 * Whether reply text could still be a step sentence rather than the answer:
 * one line, no markdown block, short. Grows monotonically out of this while a
 * segment streams (text only gets longer), so no sticky flag is needed.
 */
export function isStepCandidate(text) {
	const t = String(text || "").trim();
	if (!t || t.length > MAX_CANDIDATE_CHARS || t.includes("\n")) return false;
	return !BLOCK_START.test(t);
}

/**
 * Split the streamed reply text into what types into the current step
 * (narration the relay has not classified yet) and what shows as the answer.
 *
 * Short one-line text types into the box; the relay then either moves it into
 * the step list (a run:step with the same sentence plus an empty mirror) or it
 * outgrows a step and becomes the answer. At the end of the run whatever is
 * left is the answer, however short. This is what stops the box folding and
 * reopening for every sentence of narration.
 *
 * @param {string} shown the reply text the relay published (steps removed)
 * @param {{ended?: boolean}} [opts]
 * @returns {{candidate: string, answer: string}}
 */
export function splitNarration(shown, { ended = false } = {}) {
	const text = String(shown || "");
	if (!ended && isStepCandidate(text))
		return { candidate: text.trim().replace(INLINE_MARK, ""), answer: "" };
	return { candidate: "", answer: text };
}

/**
 * The narration typing into the box after a reply delta (ChatView's
 * liveCandidate). `next` is this delta's split: {runId, msgId, text (the
 * candidate), answer, full (the reply text)}.
 *
 * The relay hides narration from the reply one frame before it sends the same
 * sentence as a step (the hide delta, then run:step). Clearing the typing row
 * on the hide made the box drop a row, or fall back to "Thinking" or
 * "Preparing your session", for that frame. So when a delta of the same run
 * empties the reply while a sentence was typing, the sentence is HELD on
 * screen until run:step (or the next text) replaces it. A held candidate has
 * no `full`: a terminal must never snap hidden narration in as the answer.
 */
export function candidateAfterDelta(prev, next) {
	const { runId = null, msgId = null, text = "", answer = "", full = "" } = next || {};
	const hidden =
		!text &&
		!String(answer).trim() &&
		!!(prev && prev.text) &&
		prev.runId === runId &&
		prev.msgId === msgId;
	if (hidden) return { runId, msgId, text: prev.text, full: "", held: true };
	return { runId, msgId, text, full, held: false };
}

/**
 * Whether reply `msgName` already has saved tool rows the MODEL ran (they
 * follow it until the next user row): a reload or a tab-focus resync mid-turn
 * loads them, and they mean the turn has been working. The plugin's own memory
 * recall at the start of a first turn (saved without a call id about half a
 * second in) is session setup, so it does not count.
 *
 * @param {Array<{name: string, role: string, tool_name?: string, tool_call_id?: string|null}>} messages
 * @param {string} msgName
 */
export function hasSavedModelTools(messages, msgName) {
	const list = Array.isArray(messages) ? messages : [];
	const at = list.findIndex((m) => m.name === msgName);
	if (at === -1) return false;
	for (let i = at + 1; i < list.length && list[i].role === "tool"; i++) {
		const row = list[i];
		if (row.tool_call_id || toolBaseName(row.tool_name) !== "recall") return true;
	}
	return false;
}

/**
 * Whether a running turn is still setting up its agent session, with nothing
 * of it on screen yet: no step, no narration typing, no tool started, no
 * answer text. The first reply pays for creating the session and loading the
 * system prompt, so the box says that instead of "Thinking".
 *
 * `sessionTurn` is the chat's first reply, or a turn the server flagged as
 * creating a new session (run:status "waking"). `activeTools` is every tool
 * started in the run, silent built-ins included. `savedTools` is true when the
 * reply already has saved tool rows the model ran (hasSavedModelTools): the
 * turn has been working, whatever this tab saw. Tool results that arrive live
 * without a tool start (the plugin's memory recall) are deliberately not an
 * input: they land about half a second into exactly these turns.
 */
export function isPreparingSession({
	sessionTurn = false,
	steps = [],
	candidate = "",
	activeTools = [],
	savedTools = false,
	answer = "",
} = {}) {
	if (!sessionTurn || savedTools) return false;
	return (
		!steps.length &&
		!String(candidate || "").trim() &&
		!activeTools.length &&
		!String(answer || "").trim()
	);
}

/**
 * The expanded head's list: each step placed before the first tool row that
 * came after it, the rows in their own order. Saved (or reloaded) steps carry
 * `at` and saved rows `creation`, both site-time strings in one format, so they
 * compare as strings; live steps and live tool rows carry `ord`, the order this
 * tab saw them in. A server-timed item sorts before every counted one: it was
 * loaded by the tab's last load, so it happened before anything seen live since
 * (a tab-focus resync mid-turn mixes the two). An item with neither puts all
 * steps first.
 *
 * @param {Array<{text: string, at?: string, ord?: number}>} steps
 * @param {Array<{name: string, creation?: string, ord?: number}>} rows
 * @returns {Array<{kind: "step", key: string, text: string} | {kind: "tool", key: string, row: object}>}
 */
export function activityItems(steps, rows) {
	const list = (Array.isArray(steps) ? steps : []).filter((s) => s && s.text);
	const tools = Array.isArray(rows) ? rows : [];
	const stepItem = (s, i) => ({ kind: "step", key: `step-${i}`, text: s.text });
	const toolItem = (row) => ({ kind: "tool", key: row.name, row });
	const rank = (v) => (typeof v === "string" && v ? [0, v] : Number.isFinite(v) ? [1, v] : null);
	const stepKey = (s) => rank(s.at != null ? s.at : s.ord);
	const rowKey = (r) => rank(r.ord != null ? r.ord : r.creation);
	const notAfter = (a, b) => (a[0] !== b[0] ? a[0] < b[0] : a[1] <= b[1]);
	if ([...list.map(stepKey), ...tools.map(rowKey)].some((k) => !k))
		return [...list.map(stepItem), ...tools.map(toolItem)];
	const out = [];
	let next = 0;
	for (const row of tools) {
		while (next < list.length && notAfter(stepKey(list[next]), rowKey(row))) {
			out.push(stepItem(list[next], next));
			next++;
		}
		out.push(toolItem(row));
	}
	for (; next < list.length; next++) out.push(stepItem(list[next], next));
	return out;
}

/** "1 tool" / "5 tools": every tool the turn ran (turnToolNames). */
export function countLabel(toolNames) {
	const n = Array.isArray(toolNames) ? toolNames.length : 0;
	if (!n) return "";
	return `${n} tool${n === 1 ? "" : "s"}`;
}

/**
 * Every tool a turn ran, for the folded head's count: the saved tool rows plus
 * any tool seen live that has no saved row yet (the rows only arrive with the
 * enrichment reload, and a mid-turn reload loads just the ones saved so far).
 *
 * A saved row consumes the live entry with its tool call id; failing that, one
 * live entry of the same tool whose id no other saved row claims (a jarvis
 * call saved without its id, or a live event that carried none). So a tool is
 * never counted twice, whichever side lost its id.
 *
 * @param {Array<{tool_name: string, tool_call_id?: string|null}>} rows
 * @param {Array<{id?: string, name: string}>} liveTools
 * @returns {string[]}
 */
export function turnToolNames(rows, liveTools) {
	const saved = Array.isArray(rows) ? rows : [];
	const live = [...(Array.isArray(liveTools) ? liveTools : [])];
	const savedIds = new Set(saved.map((r) => r.tool_call_id).filter(Boolean));
	for (const row of saved) {
		let at = row.tool_call_id ? live.findIndex((t) => t.id === row.tool_call_id) : -1;
		if (at === -1)
			at = live.findIndex(
				(t) => !savedIds.has(t.id) && toolBaseName(t.name) === toolBaseName(row.tool_name)
			);
		if (at !== -1) live.splice(at, 1);
	}
	return [...saved.map((r) => r.tool_name), ...live.map((t) => t.name)];
}

/** Whole seconds under a minute ("48s"), then minutes ("3m 12s", "4m"). */
export function formatWorked(seconds) {
	const s = Number(seconds);
	if (!Number.isFinite(s) || s <= 0) return "";
	if (s < 60) return `${Math.max(1, Math.round(s))}s`;
	const total = Math.round(s);
	const mm = Math.floor(total / 60);
	const ss = total % 60;
	return ss ? `${mm}m ${ss}s` : `${mm}m`;
}

/**
 * The folded head of a finished (or answering) turn, which is also the saved
 * activity strip. Returns null when there is nothing worth a line (an old
 * reply with no duration and no tools).
 *
 * @param {{seconds?: number|string, toolNames?: string[], finishing?: boolean,
 *   stopped?: boolean, failed?: boolean, showDetail?: boolean}} input
 * @returns {null|{label: string, count: string, finishing: boolean,
 *   subline: string, expandable: boolean, tone: "done"|"stopped"|"failed"}}
 */
export function foldedHead({
	seconds,
	toolNames = [],
	finishing = false,
	stopped = false,
	failed = false,
	showDetail = true,
} = {}) {
	const worked = formatWorked(seconds);
	const verb = stopped ? "Stopped" : failed ? "Failed" : "Worked";
	let label = "";
	if (worked) label = stopped || failed ? `${verb} after ${worked}` : `${verb} ${worked}`;
	else if (stopped || failed) label = verb;
	const count = showDetail ? countLabel(toolNames) : "";
	if (!label && !count && !finishing) return null;
	return {
		label,
		count,
		finishing: !!finishing,
		subline: stopped ? "You stopped this reply." : "",
		expandable: !!showDetail && toolNames.length > 0,
		tone: stopped ? "stopped" : failed ? "failed" : "done",
	};
}

/**
 * The sub-line under the current step: what is happening right now, then the
 * elapsed time. With activity detail off only the time shows.
 */
export function stepSubline({
	toolPhrase = "",
	analyzing = false,
	elapsed = "",
	showDetail = true,
}) {
	const what = showDetail ? toolPhrase || (analyzing ? "Reading results" : "") : "";
	if (what && elapsed) return `${what} · ${elapsed}`;
	return what || elapsed || "";
}

/**
 * The live box for a running turn.
 *
 * Signals are ChatView's existing refs, passed in plainly:
 *   steps        recorded steps for this run, [{text, slot}] (addStep)
 *   candidate    narration typing into the current step (splitNarration)
 *   activeTools  [{name, status}] for this run
 *   waiting, statusPhase, artifactKind, elapsed, showDetail
 *   toolPhrase   the plain phrase for the running tool ("Opening Sales Invoice…")
 *   override     {text, sub} for a state that owns the current step:
 *                recovering, mid-turn compacting, the goto redirect, pre-connect
 *   idleText     what the current step says before any narration ("Thinking")
 *
 * @returns {{header: null|{kind: string, title: string},
 *   rows: Array<{text: string, state: "done"|"current"|"upcoming", sub?: string}>,
 *   earlier: number, announce: string}}
 */
export function liveBox({
	steps = [],
	candidate = "",
	activeTools = [],
	waiting = false,
	statusPhase = null,
	artifactKind = null,
	elapsed = "",
	showDetail = true,
	toolPhrase = "",
	override = null,
	idleText = "Thinking",
} = {}) {
	const phases = artifactKind ? artifactPhaseList(artifactKind) : [];
	const phaseKey = artifactKind
		? artifactBuildPhase(artifactKind, { activeTools, statusPhase, waiting })
		: null;
	const phaseAt = phases.findIndex((p) => p.key === phaseKey);

	// Narration still typing joins the list as the newest step (in the current
	// phase's slot on a file turn) unless the relay already recorded it.
	const narration = (Array.isArray(steps) ? steps : []).filter((s) => s && s.text);
	const typing = String(candidate || "").trim();
	const last = narration[narration.length - 1];
	if (typing && last && typing.startsWith(last.text))
		narration[narration.length - 1] = { ...last, text: typing };
	else if (typing && !(last && last.text.startsWith(typing)))
		narration.push({ text: typing, slot: phaseAt >= 0 ? phaseAt : null });

	const analyzing =
		statusPhase === "analyzing" ||
		(activeTools.length > 0 && !activeTools.some((t) => t && t.status === "running"));
	const sub = override
		? [override.sub, elapsed].filter(Boolean).join(" · ")
		: stepSubline({ toolPhrase, analyzing, elapsed, showDetail });

	let rows;
	let header = null;
	if (artifactKind) {
		// File and dashboard turns: the phases are the default steps and the
		// model's narration takes the place of the phase it arrived during.
		const { slots, lastAt, length } = placeNarration(narration, phases.length);
		const current = Math.max(phaseAt, lastAt, 0);
		rows = [];
		for (let i = 0; i < Math.max(length, current + 1); i++) {
			const text = slots[i] || (phases[i] && phases[i].label) || "";
			const state = i < current ? "done" : i === current ? "current" : "upcoming";
			rows.push({ text, state });
		}
		header = { kind: artifactKind, title: ARTIFACT_TITLES[artifactKind] || "" };
	} else if (narration.length) {
		rows = narration.map((s, i) => ({
			text: s.text,
			state: i === narration.length - 1 ? "current" : "done",
		}));
	} else {
		rows = [{ text: idleText, state: "current" }];
	}

	if (override && override.text) {
		// Recovery, compaction, the goto redirect: a new current step after the
		// steps already done, so what happened so far stays on screen. Upcoming
		// artifact phases and the idle placeholder are dropped.
		const kept = artifactKind
			? rows.slice(
					0,
					rows.findIndex((r) => r.state === "current")
			  )
			: narration.length
			? rows
			: [];
		rows = [
			...kept.map((r) => ({ ...r, state: "done" })),
			{ text: override.text, state: "current" },
		];
	}

	const cur = rows.findIndex((r) => r.state === "current");
	rows[cur] = { ...rows[cur], sub };
	const firstShown = Math.max(0, cur - VISIBLE_DONE_STEPS);
	return {
		header,
		rows: rows.slice(firstShown),
		earlier: firstShown,
		announce: rows[cur].text,
	};
}
