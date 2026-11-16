// What the shared draft card draws for one stored draft. Pure: no Vue, no DOM, relative
// imports only, so the desktop SPA, the phone PWA and bare `node --test` all run it.
//
// `draftCardView(card, ctx)` never throws; a card it cannot read becomes state S12.
//
// It returns only the CARD states of the State Table: S1 ready, S2 earlier, S3 editing,
// S4 working, S11 sensitive, S12 unreadable, S13 turn running. The others are not card
// states: S5 (fix needed) lives in the edit form, S6 to S10 are receipts drawn elsewhere.
//
// ROUTING: a sensitive card (state S11, `useLegacyCard: true`) is NOT drawn by DraftCard. The
// view carries no content and no buttons for it, because the existing PendingCard shows more
// (line diffs, from/to tables, consequence, warning) and a sensitive change must never be
// confirmed with less detail. Hosts check `view.state === "S11"` (or `isSensitiveCard(card)`)
// and mount their PendingCard, whose buttons read Confirm / Discard. `isDraftCard(card)` is
// true only for a readable, non-sensitive draft.
//
// ctx = { earlier, busy: "create"|"cancel"|"busy"|""|null, editing, streaming, surface: "desktop"|"phone",
//         readOnly }
// `readOnly: true` draws Preview and Edit disabled in the ready states (S1, S2), so the layout
// does not move when a host that cannot open them yet starts to. Every other button and state
// is unchanged by it.
// Precedence when several flags are true:
//   unreadable > sensitive > busy > streaming > editing > earlier > ready
import { typedApprovalHint } from "./typedCardReply.js";
import { cardWarningOf } from "./actionSummary.js";

export const UNREADABLE_TEXT = "Couldn't show this draft. Ask Jarvis to send it again.";

// A table shows this many rows, then "N more". Fewer on a phone so a card stays short.
export const MAX_ROWS_DESKTOP = 8;
export const MAX_ROWS_PHONE = 5;

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);
// Objects and arrays never print as "[object Object]"; "null" and "undefined" count as empty.
const text = (v) => {
	if (v === null || v === undefined || typeof v === "object") return "";
	const t = String(v).trim();
	return t === "null" || t === "undefined" ? "" : t;
};
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const TOOL_NAME = /\b(create|update)_doc\b/i;

function isReadable(card) {
	if (!isObject(card) || !isObject(card.draft)) return false;
	const d = card.draft;
	return (d.verb === "create" || d.verb === "update") && isObject(d.values);
}

// True only for a readable, non-sensitive draft (the S1 family).
export function isDraftCard(card) {
	return isReadable(card) && !card.risk;
}

// True for any object card with `risk`: the host draws its legacy PendingCard.
export function isSensitiveCard(card) {
	return isObject(card) && !!card.risk;
}

function pressed(id, label, opts = {}) {
	return { id, label, primary: false, disabled: false, loading: false, ...opts };
}

// The buttons of a ready card, in tab order: the one primary first.
function readyButtons(verb, ctx = {}) {
	const readOnly = ctx.readOnly === true;
	return [
		pressed(
			verb === "update" ? "save" : "create",
			verb === "update" ? "Save changes" : "Create",
			{
				primary: true,
			}
		),
		pressed("preview", "Preview", { disabled: readOnly }),
		pressed("edit", "Edit", { disabled: readOnly }),
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

// The pressed button spins: Cancel for busy "cancel", the primary for busy "create". Any other
// truthy busy ("busy": in flight, button unknown) locks every button and spins none.
function isPressed(b, ctx) {
	if (ctx.busy === "cancel") return b.id === "cancel";
	return ctx.busy === "create" && b.primary;
}

// A column is dropped only when every cell is empty. "No", "0" and false are real values the
// agent drafted and the person must see before pressing Create. If every column would go, the
// first one stays so the rows can still be told apart.
function usefulColumns(columns, rows) {
	const keep = [];
	columns.forEach((_, i) => {
		if (!rows.every((r) => text(r[i]) === "")) keep.push(i);
	});
	return keep.length || !columns.length ? keep : [0];
}

function tableView(t, surface) {
	const rows = (Array.isArray(t.rows) ? t.rows : []).map((r) =>
		Array.isArray(r && r.cells) ? r.cells : []
	);
	// A row wider than the header keeps its extra cells under an empty header.
	const width = Math.max(
		Array.isArray(t.columns) ? t.columns.length : 0,
		...rows.map((r) => r.length)
	);
	const columns = Array.from({ length: width }, (_, i) => text((t.columns || [])[i]));
	const keep = usefulColumns(columns, rows);
	const max = surface === "phone" ? MAX_ROWS_PHONE : MAX_ROWS_DESKTOP;
	const shown = rows.slice(0, max);
	const count = Math.max(Number.isInteger(t.count) && t.count >= 0 ? t.count : 0, shown.length);
	return {
		label: text(t.label),
		columns: keep.map((i) => columns[i]),
		rows: shown.map((r) => keep.map((i) => text(r[i]))),
		count,
		more: Math.max(0, count - shown.length),
		moreColumns:
			Number.isInteger(t.extra_columns) && t.extra_columns > 0 ? t.extra_columns : 0,
	};
}

function rowsView(card) {
	return (Array.isArray(card.rows) ? card.rows : [])
		.map((r) => ({ label: text(r && r.label), value: text(r && r.value) }))
		.filter((r) => r.value !== "" && r.value !== "-" && r.value !== "null" && r.label !== "");
}

const STATUS_LABEL = { changed: "Changed", new: "New" };
const VERIFY_NOTE = "This table could not be fully compared. Check its rows before you save.";
const HIDDEN_CHANGE_NOTE = "A value not shown here changes.";
const hiddenChange = (r) => isObject(r) && r.hidden_change === true;
const countOf = (n) => (Number.isInteger(n) && n >= 0 ? n : null);
const asList = (v) => (Array.isArray(v) ? v : []);
const cellsOf = (r) => asList(isObject(r) ? r.cells : null);
const statusOf = (r) => {
	const s = isObject(r) ? r.status : "";
	return s === "changed" || s === "new" ? s : "kept";
};

// "1 row changed, 2 rows added, 1 row will be removed, 3 rows unchanged": non-zero parts only.
// Per part the larger of the server's count and what the lists show, so the sentence never says
// fewer rows than the card lists. `moreRemoved` is the server's removed rows beyond its list.
function changeSummary(rc, rows, removed, moreRemoved, reordered) {
	const given = isObject(rc.counts) ? rc.counts : {};
	const of = (k, derived) => Math.max(countOf(given[k]) || 0, derived);
	const inRows = (s) => rows.filter((r) => statusOf(r) === s).length;
	const n = {
		changed: of("changed", inRows("changed")),
		new: of("new", inRows("new")),
		kept: of("kept", inRows("kept")),
		removed: of("removed", removed.length + moreRemoved),
	};
	const row = (c) => (c === 1 ? "row" : "rows");
	const parts = [
		n.changed && `${n.changed} ${row(n.changed)} changed`,
		n.new && `${n.new} ${row(n.new)} added`,
		n.removed && `${n.removed} ${row(n.removed)} will be removed`,
		n.kept && `${n.kept} ${row(n.kept)} unchanged`,
		reordered && "row order changes",
	].filter(Boolean);
	const sentence = parts.join(", ");
	// Defensive: the row-order part is lowercase in a list, so a lone one is capitalised.
	// Kept in case a server ever sends a reorder with all counts zero.
	return parts.length === 1 && reordered ? "Row order changes" : sentence;
}

// Changed and new rows are always shown (the server sends at most 50); unchanged rows only fill
// what is left of the per-surface cap. The call's order is kept among the shown rows.
function shownRows(rows, max) {
	const room = Math.max(0, max - rows.filter((r) => statusOf(r) !== "kept").length);
	let kept = 0;
	return rows.filter((r) => statusOf(r) !== "kept" || kept++ < room);
}

// What an update does to a child table, row by row. Null when there is nothing to draw, so the
// plain "3 rows -> 3 rows" text stays. A column goes only when it is empty in every row, every
// old value and every removed row. Rows shown are capped per surface, rows and removed apart.
function rowChangesView(rc, surface) {
	if (!isObject(rc)) return null;
	const rows = asList(rc.rows);
	const removed = asList(rc.removed);
	if (!rows.length && !removed.length) return null;
	const was = (r) => (statusOf(r) === "changed" ? asList(r.was) : []);
	const grids = [...rows.map(cellsOf), ...rows.map(was), ...removed.map(cellsOf)];
	const head = asList(rc.columns);
	const width = Math.max(head.length, ...grids.map((g) => g.length));
	const keep = [];
	for (let i = 0; i < width; i++) if (!grids.every((g) => text(g[i]) === "")) keep.push(i);
	if (!keep.length) keep.push(0);
	const max = surface === "phone" ? MAX_ROWS_PHONE : MAX_ROWS_DESKTOP;
	const shown = shownRows(rows, max);
	const shownRemoved = removed.slice(0, max);
	const reordered = rc.reordered === true;
	const unverified = rc.unverified === true;
	const given = isObject(rc.more) ? rc.more : {};
	const hiddenKept = rows.length - shown.length;
	const moreRemoved = removed.length - shownRemoved.length;
	const serverRemoved = countOf(given.removed) || 0;
	const serverRows = countOf(given.rows) || 0;
	return {
		columns: keep.map((i) => text(head[i])),
		summary: changeSummary(rc, rows, removed, serverRemoved, reordered),
		reordered,
		unverified,
		verifyNote: unverified ? VERIFY_NOTE : "",
		keptLabel: unverified ? "Not compared" : "Unchanged",
		rows: shown.map((r) => {
			const cells = keep.map((i) => {
				const now = text(cellsOf(r)[i]);
				const old = statusOf(r) === "changed" && !hiddenChange(r) ? text(was(r)[i]) : "";
				return { text: now, was: old === now ? "" : old };
			});
			// "Changed" always comes with a reason: a hidden value, or a visible old value.
			const unexplained = statusOf(r) === "changed" && !cells.some((c) => c.was !== "");
			return {
				status: statusOf(r),
				statusLabel: STATUS_LABEL[statusOf(r)] || "",
				note: hiddenChange(r) || unexplained ? HIDDEN_CHANGE_NOTE : "",
				cells,
			};
		}),
		removed: shownRemoved.map((r) => ({ cells: keep.map((i) => text(cellsOf(r)[i])) })),
		more: [
			hiddenKept && `and ${plural(hiddenKept, "more unchanged row", "more unchanged rows")}`,
			moreRemoved + serverRemoved &&
				`and ${moreRemoved + serverRemoved} more ${
					moreRemoved + serverRemoved === 1 ? "row" : "rows"
				} will be removed`,
		]
			.filter(Boolean)
			.join(", "),
		notShown: serverRows ? `and ${plural(serverRows, "more row", "more rows")} not shown` : "",
	};
}

// A diff entry may carry `to_table` (rows the draft adds); only those new rows are drawn, under
// the label "New rows". `from_table` and `lines` are not drawn on a normal draft card.
// `row_changes` (an update of a child table) replaces the entry's "3 rows -> 3 rows" text.
function diffView(card, surface) {
	return (Array.isArray(card.diff) ? card.diff : []).filter(isObject).map((d) => {
		const rowChanges = rowChangesView(d.row_changes, surface);
		return {
			label: text(d.label),
			old: rowChanges ? "" : text(d.from),
			new: rowChanges ? "" : text(d.to),
			multiline: d.multiline === true,
			rowChanges,
			// The note under the entry: the row-changes table's own, or the server's flag on
			// the entry itself (a child table it could not compare at all; strict `=== true`).
			verifyNote:
				(rowChanges && rowChanges.verifyNote) ||
				(d.unverified === true ? VERIFY_NOTE : ""),
			table: isObject(d.to_table)
				? tableView({ ...d.to_table, label: "New rows" }, surface)
				: null,
		};
	});
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
		if (t.count)
			parts.push(plural(t.count, "row", "rows") + (t.label ? ` in ${t.label}` : ""));
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
	warning: "",
	buttons: [],
	hint: "",
	footer: "",
	tag: "",
	message: "",
	useLegacyCard: false,
};

// Cancel is the only way out of an unreadable card: it stays live unless a request is in flight.
function unreadable(ctx = {}) {
	return {
		...EMPTY,
		buttons: [
			pressed("cancel", "Cancel", { disabled: !!ctx.busy, loading: ctx.busy === "cancel" }),
		],
		message: UNREADABLE_TEXT,
	};
}

// No content, no buttons: see ROUTING above.
function sensitive() {
	return { ...EMPTY, state: "S11", useLegacyCard: true };
}

function stateOf(ctx) {
	if (ctx.busy) return "S4";
	if (ctx.streaming) return "S13";
	if (ctx.editing) return "S3";
	if (ctx.earlier) return "S2";
	return "S1";
}

function build(card, ctx) {
	if (isSensitiveCard(card)) return sensitive();
	if (!isReadable(card)) return unreadable(ctx);
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
		diff: draft.verb === "update" ? diffView(card, ctx.surface) : [],
		warning: cardWarningOf(card),
		ignoredNote: noteFor(draft),
		buttons: ready
			? readyButtons(draft.verb, ctx)
			: lockedButtons(readyButtons(draft.verb, ctx), ctx, state),
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
