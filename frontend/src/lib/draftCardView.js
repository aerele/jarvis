// What the shared draft card draws for one stored draft. Pure: no Vue, no DOM, relative
// imports only, so the desktop SPA, the phone PWA and bare `node --test` all run it.
//
// `draftCardView(card, ctx)` never throws; a card it cannot read becomes state S12.
//
// It returns only the CARD states of the State Table: S1 ready, S2 earlier, S3 editing,
// S4 working, S11 sensitive, S12 unreadable, S13 turn running. The others are not card
// states: S5 (fix needed) lives in the edit form, S6 to S10 are receipts drawn elsewhere.
//
// ctx = { earlier, busy: "create"|"cancel"|""|null, editing, streaming, surface: "desktop"|"phone" }
// Precedence when several flags are true:
//   unreadable > sensitive > busy > streaming > editing > earlier > ready
import { typedApprovalHint } from "./typedCardReply.js";

export const UNREADABLE_TEXT = "Couldn't show this draft. Ask Jarvis to send it again.";

// A table shows this many rows, then "N more". Fewer on a phone so a card stays short.
export const MAX_ROWS_DESKTOP = 8;
export const MAX_ROWS_PHONE = 5;

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v) => (v === null || v === undefined ? "" : String(v).trim());
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const TOOL_NAME = /\b(create|update)_doc\b/i;

function isReadable(card) {
	if (!isObject(card) || !isObject(card.draft)) return false;
	const d = card.draft;
	return (d.verb === "create" || d.verb === "update") && isObject(d.values);
}

function pressed(id, label, opts = {}) {
	return { id, label, primary: false, disabled: false, loading: false, ...opts };
}

// The buttons of a ready card, in tab order: the one primary first.
function readyButtons(verb) {
	return [
		pressed(
			verb === "update" ? "save" : "create",
			verb === "update" ? "Save changes" : "Create",
			{
				primary: true,
			}
		),
		pressed("preview", "Preview"),
		pressed("edit", "Edit"),
		pressed("cancel", "Cancel"),
	];
}

// S3 leaves Cancel live (it closes the editor); S4 and S13 disable everything.
function lockedButtons(buttons, ctx, state) {
	return buttons.map((b) => ({
		...b,
		disabled: state === "S3" ? b.id !== "cancel" : true,
		loading: isPressed(b, ctx),
	}));
}

// The pressed button spins: Cancel for busy "cancel", the primary for anything else.
function isPressed(b, ctx) {
	return !!ctx.busy && (ctx.busy === "cancel" ? b.id === "cancel" : b.primary);
}

// A column is dropped when every cell is empty, or every cell is the word "No" (an optional
// flag nobody set, e.g. Is Billable). Zero is kept: "0" hours or "0.00" amount says something.
// If that would drop every column, the first one stays so the rows can still be told apart.
function usefulColumns(columns, rows) {
	const keep = [];
	columns.forEach((_, i) => {
		const cells = rows.map((r) => text(r[i]));
		if (!cells.every((c) => c === "" || c.toLowerCase() === "no")) keep.push(i);
	});
	return keep.length || !columns.length ? keep : [0];
}

function tableView(t, surface) {
	const columns = Array.isArray(t.columns) ? t.columns.map(text) : [];
	const rows = (Array.isArray(t.rows) ? t.rows : []).map((r) =>
		Array.isArray(r && r.cells) ? r.cells : []
	);
	const keep = usefulColumns(columns, rows);
	const max = surface === "phone" ? MAX_ROWS_PHONE : MAX_ROWS_DESKTOP;
	const shown = rows.slice(0, max);
	const count = Number.isFinite(t.count) ? t.count : rows.length;
	return {
		label: text(t.label),
		columns: keep.map((i) => columns[i]),
		rows: shown.map((r) => keep.map((i) => text(r[i]))),
		count,
		more: Math.max(0, count - shown.length),
	};
}

function rowsView(card) {
	return (Array.isArray(card.rows) ? card.rows : [])
		.map((r) => ({ label: text(r && r.label), value: text(r && r.value) }))
		.filter((r) => r.value !== "" && r.value !== "-" && r.value !== "null" && r.label !== "");
}

function diffView(card) {
	return (Array.isArray(card.diff) ? card.diff : [])
		.filter(isObject)
		.map((d) => ({ label: text(d.label), old: text(d.from), new: text(d.to) }));
}

function labels(list) {
	return (Array.isArray(list) ? list : []).map(text).filter(Boolean).join(", ");
}

function noteFor(draft) {
	const left = labels(draft.ignored);
	const dropped = labels(draft.dropped);
	const parts = [];
	if (left) parts.push(`Left out: ${left}. These are not fields on this form.`);
	if (dropped) parts.push(`Not saved because you can't edit them: ${dropped}.`);
	return parts.join(" ");
}

function titleFor(draft) {
	const given = text(draft.title);
	if (given && !TOOL_NAME.test(given)) return given;
	const doctype = text(draft.doctype) || "record";
	return draft.verb === "update" ? `${doctype} ${text(draft.name)}`.trim() : `New ${doctype}`;
}

function summaryFor(draft, tables) {
	const values = draft.values;
	const fields = Object.keys(values).filter((k) => !Array.isArray(values[k])).length;
	const parts = [];
	if (fields) parts.push(plural(fields, "field", "fields"));
	for (const t of tables)
		if (t.count) parts.push(`${plural(t.count, "row", "rows")} in ${t.label}`);
	const doctype = text(draft.doctype) || "record";
	if (draft.verb === "update") {
		const target = `${doctype} ${text(draft.name)}`.trim();
		return parts.length ? `Changes ${parts.join(", ")} on ${target}` : `Changes ${target}`;
	}
	return parts.length ? `${doctype}: ${parts.join(", ")}` : doctype;
}

const EMPTY = {
	state: "S12",
	title: "",
	summary: "",
	rows: [],
	tables: [],
	diff: [],
	ignoredNote: "",
	buttons: [],
	hint: "",
	footer: "",
	tag: "",
	message: "",
};

function unreadable() {
	return { ...EMPTY, buttons: [pressed("cancel", "Cancel")], message: UNREADABLE_TEXT };
}

function sensitive(card, ctx) {
	const busy = !!ctx.busy || !!ctx.streaming;
	const doctype = text(card.doctype) || "record";
	const verb = text(card.kind) === "update" ? "Update" : "Create";
	const buttons = [
		pressed("create", "Confirm", { primary: true }),
		pressed("cancel", "Discard"),
	].map((b) => ({
		...b,
		disabled: busy,
		loading: isPressed(b, ctx),
	}));
	return {
		...EMPTY,
		state: "S11",
		title: text(card.title) || `${verb} ${doctype}`,
		summary: text(card.risk_line),
		rows: rowsView(card),
		diff: diffView(card),
		buttons,
	};
}

function stateOf(ctx) {
	if (ctx.busy) return "S4";
	if (ctx.streaming) return "S13";
	if (ctx.editing) return "S3";
	if (ctx.earlier) return "S2";
	return "S1";
}

function build(card, ctx) {
	if (isObject(card) && card.risk) return sensitive(card, ctx);
	if (!isReadable(card)) return unreadable();
	const draft = card.draft;
	const state = stateOf(ctx);
	const tables = (Array.isArray(card.tables) ? card.tables : [])
		.filter(isObject)
		.map((t) => tableView(t, ctx.surface));
	const ready = state === "S1" || state === "S2";
	return {
		...EMPTY,
		state,
		title: titleFor(draft),
		summary: summaryFor(draft, tables),
		rows: rowsView(card),
		tables,
		diff: draft.verb === "update" ? diffView(card) : [],
		ignoredNote: noteFor(draft),
		buttons: ready
			? readyButtons(draft.verb)
			: lockedButtons(readyButtons(draft.verb), ctx, state),
		hint: ready ? typedApprovalHint([{ recent: state === "S1" }]) : "",
		footer: state === "S3" ? "Editing" : state === "S13" ? "Jarvis is replying" : "",
		tag: state === "S2" ? "Earlier" : "",
		message: "",
	};
}

export function draftCardView(card, ctx) {
	try {
		return build(card, isObject(ctx) ? ctx : {});
	} catch {
		return unreadable();
	}
}
