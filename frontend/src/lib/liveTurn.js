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
//   folded - one line once the answer is showing: "Worked 48s · 5 lookups"
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
 * phases advance. Plain turns ignore it.
 *
 * @param {Array<{text: string, slot: number|null}>} steps
 * @param {string} text
 * @param {number|null} [slot]
 * @returns {Array<{text: string, slot: number|null}>}
 */
export function addStep(steps, text, slot = null) {
	const list = Array.isArray(steps) ? steps : [];
	const t = String(text || "").trim();
	if (!t || list.some((s) => s.text === t)) return list;
	const last = list[list.length - 1];
	if (last && t.startsWith(last.text)) return [...list.slice(0, -1), { ...last, text: t }];
	if (last && last.text.startsWith(t)) return list;
	return [...list, { text: t, slot: Number.isInteger(slot) && slot >= 0 ? slot : null }];
}

/** Steps restored from the server after a reload carry no slot. */
export function stepsFromTexts(texts) {
	return (Array.isArray(texts) ? texts : []).reduce((list, t) => addStep(list, t), []);
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
	if (!ended && isStepCandidate(text)) return { candidate: text.trim(), answer: "" };
	return { candidate: "", answer: text };
}

const LOOKUP_TOOLS = new Set([
	"query",
	"run_report",
	"resolve_links",
	"read_file",
	"describe_customizations",
	"summarize_dataset",
	"recall",
	"read",
	"web_search",
	"web_fetch",
]);

/** A tool that only reads (get_list, get_doc, query, recall, preview_doc, ...). */
export function isLookupTool(name) {
	const base = toolBaseName(name);
	return (
		LOOKUP_TOOLS.has(base) || /^(get|list|search|read|describe|find|fetch|preview)_/.test(base)
	);
}

/** "1 lookup" / "5 lookups", or "actions" once anything in the turn writes. */
export function countLabel(toolNames) {
	const names = Array.isArray(toolNames) ? toolNames : [];
	if (!names.length) return "";
	const n = names.length;
	const noun = names.every(isLookupTool) ? "lookup" : "action";
	return `${n} ${noun}${n === 1 ? "" : "s"}`;
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
