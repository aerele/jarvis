// The pure parts of editing and previewing a SERVER draft card on the desktop: which form field
// a drafted key is, the model's stamp and the snapshot guard, the card's own computed cells for
// Preview, and where the first mark of a refused save is. No Vue, no API; relative imports only.
// The host wiring (the doors, the busy state) is lib/toolDraftHost.js; ChatView.vue only calls it.
import { snapshotDraftModel, snapshotMatchesCard } from "./draftCardValues.js";
import { stampDraftModel } from "./draftSave.js";

export const CANT_EDIT_TEXT =
	"This draft can't be edited here. Use Create on the card, or ask Jarvis to change it.";
export const STORED_UNAVAILABLE_TEXT =
	"The saved values of this record could not be loaded, so the form shows only what Jarvis drafted.";

// Marks an action that was made from a server draft card (draftFromCard output handed to the
// model builder by the host). A Symbol, so a typed action's JSON, which a model writes, can never
// carry it: the typed path keeps resolving by label and keeps refusing an unloadable record.
export const FROM_CARD = Symbol("jarvis.fromCard");

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// Which form field a drafted entry belongs to. With `exact` (the action came from a server draft
// card, `action[FROM_CARD]`) an entry is the field its `fieldname` names, EXACTLY: no match in the
// form's meta leaves the key out of the form (the server keeps an absent key, so nothing is
// lost) and never falls back to the label, which could land the value on another field. Without
// it (every typed action, whatever keys the model wrote) the entry's label is resolved as before:
// `byLabel(label)`.
export function resolveDraftField(entry, metaFields, byLabel, exact) {
	if (exact) {
		const name = entry && entry.fieldname;
		if (!name) return null;
		return (
			(metaFields || []).find((f) => f.fieldname === name && f.fieldtype !== "Table") || null
		);
	}
	return byLabel(entry && entry.label);
}

// Stamp a freshly built model as the form of ONE server draft card and take its snapshot. Call
// this straight after the build (the form meta, the stored record and the rule values are in),
// before the model is shown and before anything can add or remove a row: the snapshot is the
// baseline of every later diff. `guard` is snapshotMatchesCard's answer; when it is not ok the
// form may still open but `saveBlocked` carries the line shown instead of a Save (a wrong
// baseline would send a wrong save).
export function prepareToolModel(model, { token, conversation, card }) {
	const snapshot = snapshotDraftModel(model, card);
	const guard = snapshotMatchesCard(snapshot, card);
	// the card as it was when the form opened: later changes to the list item never reach the diff
	const frozen = JSON.parse(JSON.stringify(card));
	stampDraftModel(model, {
		messageKey: "",
		card: frozen,
		editable: false,
		actionKey: `tool:${token}`,
	});
	model.tool = true;
	model.token = token;
	model.conversation = conversation;
	model.snapshot = snapshot;
	model.saveBlocked = guard.ok ? "" : CANT_EDIT_TEXT;
	return { model, guard };
}

// Preview shows the numbers the CARD shows (the server's trial), never a recomputation. The
// card's table is matched to a model table only on a label that is unique on both sides, with
// the same number of rows; its cells are keyed by the column fieldnames the card carries. Any
// doubt leaves the table out and its calculated cells blank. Returns `{[table fieldname]:
// [row -> {fieldname: text}]}` (sparse), the shape DraftPreview's `model.computed` takes.
export function computedFromCard(model, card) {
	const out = {};
	const cardTables = (card && Array.isArray(card.tables) ? card.tables : []).filter(isObject);
	const tables = (model && model.tables) || [];
	const labelOf = (t) =>
		String((t && t.label) || "")
			.trim()
			.toLowerCase();
	for (const t of tables) {
		const label = labelOf(t);
		if (!label || tables.filter((x) => labelOf(x) === label).length !== 1) continue;
		const mine = cardTables.filter((c) => labelOf(c) === label);
		if (mine.length !== 1) continue;
		const ct = mine[0];
		const rows = Array.isArray(ct.rows) ? ct.rows : [];
		const count = Number.isInteger(ct.count) ? ct.count : rows.length;
		if (count !== (t.rows || []).length || rows.length > count) continue;
		const names = Array.isArray(ct.fieldnames) ? ct.fieldnames : [];
		const aligned = new Array(t.rows.length).fill(undefined);
		rows.forEach((r, i) => {
			const cells = isObject(r) && Array.isArray(r.cells) ? r.cells : [];
			const row = {};
			names.forEach((name, j) => {
				const cell = cells[j];
				if (typeof name === "string" && name && cell != null && String(cell) !== "")
					row[name] = cell;
			});
			if (Object.keys(row).length) aligned[i] = row;
		});
		if (aligned.some(Boolean)) out[t.fieldname] = aligned;
	}
	return out;
}

// The label of the form's primary button, in the card's own words.
export function toolPrimaryLabel(model) {
	return model && model.verb === "update" ? "Save changes" : "Create";
}

// A refused save marks fields and rows; the first mark in document order is where the person
// should look. These are the marks the form draws (DraftEditForm, DraftRowForm).
export const MARK_SELECTOR = ".jv-draft-fld.missing, .jv-draft-fld.invalid, .jv-rowform-missing";
const ERROR_SELECTOR = ".jv-draft-body [role=alert]";

// Scroll the first mark of `root` into view and put focus in its control. Returns the marked
// element, or null when there is no mark. Called after the DOM has caught up with the marks.
// A refusal with no mark on a field or row (a busy draft, a table the server refused as a whole)
// leaves the error banner, which is scrolled into view instead and takes no focus.
export function focusFirstMark(root) {
	const find = (sel) => (root && root.querySelector ? root.querySelector(sel) : null);
	const mark = find(MARK_SELECTOR);
	if (!mark) {
		const banner = find(ERROR_SELECTOR);
		if (banner && typeof banner.scrollIntoView === "function")
			banner.scrollIntoView({ block: "nearest", behavior: "smooth" });
		return null;
	}
	if (typeof mark.scrollIntoView === "function")
		mark.scrollIntoView({ block: "center", behavior: "smooth" });
	const control = mark.querySelector("input, select, textarea") || mark;
	if (typeof control.focus === "function") control.focus({ preventScroll: true });
	return mark;
}
