// The child-table part of the record draft card's model (ChatView's
// buildDraftModel), pulled out so it is unit-testable without mounting the view.
// One table: its grid columns (the form meta's columns plus any key the agent used
// outside them, typed from the child's meta, #655), its row form (form_fields),
// and its rows, each carrying every field of the row form so the form shows saved
// values and Apply can send what the user edits there.
import {
	allRowFields,
	coerceRow,
	isRowKeyColumn,
	overlaySavedRows,
	ruleFetchSource,
	rowKeyColumn,
	savedRowsMap,
	toPanelRow,
} from "@/lib/draftApply";
import { gridNames, rowChangedSet } from "@/lib/rowForm";

// A panel row's stable id (`__key`), unique for the page's life and never reused. The
// row's position changes when a row above it is removed; this does not, so a link
// menu, a form id and a v-for key keep pointing at the same row. Display state only:
// every payload builder walks allRowFields, so it is never sent.
let rowKeySeq = 0;
export const newRowKey = () => ++rowKeySeq;

// A blank row with every field of the table, for "Add row" and an empty table.
export function blankRow(table) {
	const o = { __key: newRowKey() };
	for (const c of allRowFields(table)) o[c.fieldname] = "";
	if ((table.ruleFields || []).length) o.__rule = ruleValues(table.ruleFields, {}, null).rule;
	return o;
}

const text = (v) => (v == null ? "" : String(v));

// What a row's rules read from the hidden fields the card shows nowhere
// (table.ruleFields): the saved or proposed value, else the field's plain default.
// A field Desk fills by fetch_from (is_fixed_asset from the item) holds the saved
// value only while the row's source link is the saved one: with another item the
// default is used, and `src` records the saved source values so the row form can
// check again as the user edits the link. Kept on the panel row (`__rule`,
// `__ruleSrc`), never in a table's fields, so Save does not send it.
// `known` is the set of fieldnames the panel row carries.
export function ruleValues(ruleFields, row, savedRow, known = new Set()) {
	const rule = {};
	const src = {};
	for (const rf of ruleFields || []) {
		let v = row[rf.fieldname] == null ? rf.default || "" : row[rf.fieldname];
		const from = ruleFetchSource(rf);
		if (from && savedRow && known.has(from)) {
			src[from] = text(savedRow[from]);
			if (text(row[from]) !== src[from]) v = rf.default || "";
		}
		rule[rf.fieldname] = v;
	}
	return { rule, src };
}

// The link values whose fetch_from rule values a table still needs (actions_api
// rule_values): per source link field, the distinct values of the rows that hold no
// value fetched or saved for their current link (a create row, or a row whose link
// moved off the saved one).
export function ruleFetchLinks(table) {
	const links = {};
	for (const rf of table.ruleFields || []) {
		const from = ruleFetchSource(rf);
		if (!from) continue;
		for (const row of table.rows || []) {
			const v = text(row[from]);
			const src = row.__ruleSrc || {};
			if (!v || (from in src && src[from] === v)) continue;
			(links[from] = links[from] || new Set()).add(v);
		}
	}
	return Object.fromEntries(Object.entries(links).map(([k, set]) => [k, [...set]]));
}

// rule_values' answer onto the rows: a fetched value becomes the row's rule value and
// its link the recorded source, so typedRow treats it as current (and falls back to
// the default again when the link changes before the next fetch). New objects, not
// in-place edits, so the row status memo sees the change.
export function applyRuleValues(table, fetched) {
	for (const rf of table.ruleFields || []) {
		const from = ruleFetchSource(rf);
		const byLink = from && fetched && fetched[from];
		if (!byLink) continue;
		for (const row of table.rows || []) {
			const v = text(row[from]);
			const got = v && byLink[v];
			if (!got || !(rf.fieldname in got)) continue;
			row.__rule = { ...(row.__rule || {}), [rf.fieldname]: got[rf.fieldname] ?? "" };
			row.__ruleSrc = { ...(row.__ruleSrc || {}), [from]: v };
		}
	}
}

// A saved or proposed row -> its panel row, plus its rule values when the table has any.
function panelRow(table, fields, r, savedByName) {
	const row = toPanelRow(fields, r);
	row.__key = newRowKey();
	if (!(table.ruleFields || []).length) return row;
	const known = new Set(fields.map((f) => f.fieldname));
	const { rule, src } = ruleValues(
		table.ruleFields,
		r,
		(r && r.name && savedByName.get(r.name)) || null,
		known
	);
	row.__rule = rule;
	if (Object.keys(src).length) row.__ruleSrc = src;
	return row;
}

// The model for one child table, or null when the card does not show it (not
// required, not proposed, and no saved rows). `proposed` is the agent's rows for
// this table (undefined when it proposed none), `saved` the loaded rows (update).
export function buildDraftTable({ fieldname, spec, required, proposed, saved = [], verb }) {
	const proposedRows = proposed && overlaySavedRows(proposed, saved);
	if (!proposedRows && !required && !saved.length) return null;
	const columns = spec.columns.slice();
	const known = new Set(columns.map((c) => c.fieldname));
	// Extra grid columns come from the keys the agent itself sent, not from the
	// overlaid rows: the overlay brings in every saved field (hidden ones too), and
	// those belong to the row form, not the grid.
	for (const r of proposed || []) {
		for (const k of Object.keys(r || {})) {
			if (!known.has(k) && isRowKeyColumn(k)) {
				known.add(k);
				columns.push(rowKeyColumn(k, spec.extra_columns));
			}
		}
	}
	const table = {
		fieldname,
		label: spec.label,
		child: spec.child_doctype,
		columns,
		// The grid as configured (before the columns the agent's own keys added).
		gridFields: spec.columns.map((c) => c.fieldname),
		formFields: spec.form_fields || [],
		// Hidden fields the row form's rules read (never edited, never sent).
		ruleFields: spec.rule_fields || [],
		rows: [],
		origJson: JSON.stringify(verb === "update" ? saved : null), // raw saved rows
	};
	// A proposal REPLACES the loaded rows. Each row keeps its saved name (__name)
	// so Apply edits THAT row (CR-3).
	const fields = allRowFields(table);
	const src = proposedRows != null ? proposedRows : saved;
	const savedByName = new Map(saved.filter((r) => r && r.name).map((r) => [r.name, r]));
	table.rows = src.map((r) => panelRow(table, fields, r, savedByName));
	if (!table.rows.length) table.rows.push(blankRow(table));
	return table;
}

// The table's saved rows by name (the parse is cached, draftApply.js).
export function savedRowsByName(table) {
	return savedRowsMap(table.origJson);
}

// The saved row a panel row came from (by its name), for showing what changed.
export function savedRowFor(table, row) {
	return (row && row.__name && savedRowsByName(table).get(row.__name)) || null;
}

// Each row's changed set (rowChangedSet), null for a row with no saved match: the
// input the auto-open and the summary below work from. Parses the saved rows once.
export function rowChangeSets(table) {
	const saved = savedRowsByName(table);
	return table.rows.map((r) => {
		const savedRow = r.__name && saved.get(r.__name);
		return savedRow ? rowChangedSet(table, r, savedRow) : null;
	});
}

// The rows of a table with changes outside the grid, as [{index, fieldnames}], from
// each row's changed set (null/empty for none): the card's way of saying "the agent
// changed this row where the grid cannot show it".
function changedOutsideGrid(table, sets) {
	// No row form (a table built without form_fields), nothing to open or to point at.
	if (!(table.formFields || []).length) return [];
	const inGrid = gridNames(table);
	const out = [];
	(sets || []).forEach((set, index) => {
		const fieldnames = set ? [...set].filter((n) => !inGrid.has(n)) : [];
		if (fieldnames.length) out.push({ index, fieldnames });
	});
	return out;
}

// Up to this many changed rows open by themselves; more would bury the card, so the
// summary offers a button instead.
export const AUTO_OPEN_MAX = 3;

// The indexes of the rows to open on the first build: 1 to 3 saved rows with changes
// outside the grid, else none. Pure; the caller sets __open and never moves focus.
export function autoOpenRows(table, sets = rowChangeSets(table)) {
	const rows = changedOutsideGrid(table, sets);
	return rows.length >= 1 && rows.length <= AUTO_OPEN_MAX ? rows.map((r) => r.index) : [];
}

// The line above the grid: null when nothing changed outside it. Up to 3 rows are
// named ("Changed outside the grid: rows 7, 12"); more are grouped by field, the
// busiest first ("Warehouse changed on 48 rows; Discount on 2 rows"), `grouped`
// telling the card to offer a button labelled `openLabel` ("Open 48 changed rows"). For
// the named case `lead` is the text before the row numbers, which the card shows as
// buttons that open each row. `rows` are the indexes to open.
export function changedSummary(table, sets) {
	const rows = changedOutsideGrid(table, sets);
	if (!rows.length) return null;
	const indexes = rows.map((r) => r.index);
	if (rows.length <= AUTO_OPEN_MAX) {
		const nums = indexes.map((i) => i + 1).join(", ");
		const word = rows.length === 1 ? "row" : "rows";
		const lead = `Changed outside the grid: ${word}`;
		return {
			text: `${lead} ${nums}`,
			lead,
			rows: indexes,
			grouped: false,
		};
	}
	const count = new Map();
	for (const r of rows) for (const n of r.fieldnames) count.set(n, (count.get(n) || 0) + 1);
	const labels = new Map(allRowFields(table).map((f) => [f.fieldname, f.label || f.fieldname]));
	const parts = [...count]
		.sort((a, b) => b[1] - a[1])
		.map(([n, c], i) => {
			const rowWord = c === 1 ? "row" : "rows";
			return i === 0
				? `${labels.get(n) || n} changed on ${c} ${rowWord}`
				: `${labels.get(n) || n} on ${c} ${rowWord}`;
		});
	return {
		text: parts.join("; "),
		rows: indexes,
		grouped: true,
		openLabel: `Open ${indexes.length} changed rows`,
	};
}

// Open the rows a summary names ("Open changed rows"). Opening only sets a flag.
export function openChangedRows(table, summary) {
	for (const i of (summary && summary.rows) || [])
		if (table.rows[i]) table.rows[i].__open = true;
}

// What the agent proposed for the saved rows, per table: for each saved row with changes
// outside the grid, its name and its changed fields as sorted `fieldname=value` pairs.
// Worked out from a freshly built model, before the user edits anything, and kept on the
// model (`__proposalKey`) so later edits to the panel never change it.
export function proposalKey(model) {
	const out = [];
	for (const t of model.tables || []) {
		const sets = rowChangeSets(t);
		for (const { index, fieldnames } of changedOutsideGrid(t, sets)) {
			const r = t.rows[index];
			const pairs = fieldnames.map((n) => `${n}=${r[n] == null ? "" : r[n]}`).sort();
			out.push([t.fieldname, r.__name, pairs]);
		}
	}
	return JSON.stringify(out);
}

// On the first build of an update draft, open the changed rows (autoOpenRows) and stamp
// the model with the agent's proposal (`__proposalKey`). `prev` is the panel that was open
// when the build ran. A rebuild closes every row, so when the agent re-emits the same
// record with the same proposal (the same values in the same fields of the same rows) the
// user has already seen it: nothing is opened again, whatever they have typed since. A
// different value or field on the same row opens by the normal rule. The message name
// cannot tell these apart, a re-emit is a new message.
// Returns what it opened, as [{table, rows}], and never touches focus.
export function applyAutoOpen(model, prev) {
	if (!model || model.verb !== "update") return [];
	model.__proposalKey = proposalKey(model);
	if (
		prev &&
		prev.verb === model.verb &&
		prev.doctype === model.doctype &&
		prev.docName === model.docName &&
		prev.__proposalKey === model.__proposalKey
	)
		return [];
	const opened = [];
	model.tables.forEach((t, ti) => {
		const rows = autoOpenRows(t);
		for (const i of rows) t.rows[i].__open = true;
		if (rows.length) opened.push({ table: ti, rows });
	});
	return opened;
}

// A row toggle's short visible text ("2 required", "3 changed"), both when both apply.
export function toggleParts({ missing = 0, changed = 0 }) {
	const parts = [];
	if (missing) parts.push({ kind: "missing", text: `${missing} required` });
	if (changed) parts.push({ kind: "changed", text: `${changed} changed` });
	return parts;
}

// The same in full words: the toggle's aria-label and its title.
export function toggleLabel(rowNumber, { missing = 0, changed = 0 }) {
	let label = `All fields of row ${rowNumber}`;
	if (missing) label += `, ${missing} required ${missing === 1 ? "field" : "fields"} blank`;
	if (changed) label += `, ${changed} ${changed === 1 ? "field" : "fields"} changed`;
	return label;
}

// The plain defaults of the record's own fields (what Desk's new form holds), so a
// child rule reading `parent.update_stock` on a create sees Desk's default.
// A Check or number default is a number, as Desk's doc holds it: the string "0" is
// truthy, and `eval:!parent.x` would read it the wrong way.
const NUMERIC_TYPES = new Set(["Check", "Int", "Float", "Currency", "Percent", "Rating"]);
export function parentDefaults(metaFields) {
	const out = {};
	for (const f of metaFields || []) {
		if (f.fieldtype === "Table" || !f.default) continue;
		const n = Number(f.default);
		out[f.fieldname] = NUMERIC_TYPES.has(f.fieldtype) && !Number.isNaN(n) ? n : f.default;
	}
	return out;
}

// The panel rows the save sent, in the order it sent them: a blank new row is never
// sent (draftValues / tableRowsPayload), so the server's row numbers count only the
// rest. A saved row is always sent on an update (by name), even unchanged.
function sentRows(table, verb) {
	return table.rows.filter(
		(r) => (verb === "update" && r.__name) || Object.keys(coerceRow(table, r, verb)).length
	);
}

// Put a failed save's missing child fields on their rows and open those rows, so
// the user sees what is missing instead of a silent failure (entries carry
// parentfield, idx and fieldname; idx counts the rows the save sent). Returns how
// many rows were marked.
export function markMissingRows(model, missing) {
	for (const t of model.tables || []) for (const r of t.rows) delete r.__missing;
	let marked = 0;
	for (const m of missing || []) {
		if (!m || !m.parentfield || !m.idx) continue;
		// A name that is not a string, or is blank, would put a nameless row in "Also needed".
		if (typeof m.fieldname !== "string" || !m.fieldname.trim()) continue;
		const t = (model.tables || []).find((x) => x.fieldname === m.parentfield);
		const row = t && sentRows(t, model.verb)[Number(m.idx) - 1];
		if (!row) continue;
		if (!row.__missing) {
			row.__missing = [];
			marked++;
		}
		row.__missing.push(m.fieldname);
		row.__open = true;
	}
	return marked;
}
