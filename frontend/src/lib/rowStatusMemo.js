// The record draft card's per-row status, kept cheap on big tables.
//
// A row's status (blank required fields, what differs from the saved row) costs
// ~0.06 ms; the cost the user feels is the card re-rendering every row because each
// status was a fresh object on every keystroke. So each row gets its own computed,
// which gives back the PREVIOUS object when nothing in the status changed (Vue 3.5
// hands a computed getter its last value). A render that reads it is then not woken:
//  - typing in row 3 recomputes row 3's status only;
//  - a keystroke that leaves the status as it was re-renders no other row;
//  - the record's own fields reach a table through ONE object holding just the
//    parent.* values that table's rules read (plus doctype, name, docstatus), so an
//    edit to a main field no rule reads recomputes no row. A watcher rewrites that
//    object only when those values change: were it a computed, a main-field edit
//    would still wake and re-check all 500 rows (~19 ms) just to find nothing new.
// The saved rows (the origJson parse) are cached by draftApply.js. What a row removal
// changes ("Saving removes N rows") is NOT cached here: it depends on the rows' names.
import { computed, effectScope, shallowRef, toRaw, watch, watchEffect } from "vue";
import { draftParentDoc } from "@/lib/draftApply";
import { changedSummary, savedRowsByName } from "@/lib/draftTables";
import { rowStatus } from "@/lib/rowForm";

// The `parent.<name>` values a table's rules read, or null when a rule reads `parent`
// any other way (`parent[x]`, passed whole): then the table gets every parent value.
// Rule texts are eval:<js>; a bare fieldname rule reads `doc`, never `parent`.
const PARENT_READ = /(?<![.\w$])parent\s*\.\s*([A-Za-z_$][\w$]*)/g;
const PARENT_ANY = /(?<![.\w$])parent(?![\w$])/g;
const RULES = ["depends_on", "mandatory_depends_on", "read_only_depends_on"];
export function ruleParentKeys(table) {
	const names = new Set();
	for (const f of table.formFields || [])
		for (const k of RULES) {
			const rule = String(f[k] || "");
			if (!rule.startsWith("eval:")) continue;
			const named = [...rule.matchAll(PARENT_READ)];
			if (named.length !== [...rule.matchAll(PARENT_ANY)].length) return null;
			for (const m of named) names.add(m[1]);
		}
	return names;
}

// Whether a table's rules read the row's own place (`doc.idx`, or `doc` any way that
// could): only then does a row's status depend on where the row is.
const DOC_IDX = /(?<![.\w$])doc\s*\.\s*idx(?![\w$])/;
const DOC_NAMED = /(?<![.\w$])doc\s*\.\s*[A-Za-z_$]/g;
const DOC_ANY = /(?<![.\w$])doc(?![\w$])/g;
export function ruleReadsIdx(table) {
	for (const f of table.formFields || [])
		for (const k of RULES) {
			const rule = String(f[k] || "");
			if (rule.trim() === "idx") return true;
			if (!rule.startsWith("eval:")) continue;
			if (DOC_IDX.test(rule)) return true;
			if ([...rule.matchAll(DOC_NAMED)].length !== [...rule.matchAll(DOC_ANY)].length)
				return true;
		}
	return false;
}

function sameSet(a, b) {
	if (a === b) return true;
	if (!a || !b || a.size !== b.size) return false;
	for (const x of a) if (!b.has(x)) return false;
	return true;
}

// Whether a recomputed status says the same as the last one (old is undefined on the
// first run).
function sameStatus(n, old) {
	return (
		!!old &&
		n.missing === old.missing &&
		n.changed === old.changed &&
		n.savedRow === old.savedRow &&
		sameSet(n.changedFields, old.changedFields)
	);
}

// `getPanel` returns the draft model (reactive), or null when no card is open.
export function createRowStatusMemo(getPanel) {
	// The watchers below live in a scope of their own: part of the calling component's
	// when made in its setup, so they stop when it unmounts; `dispose` for any other use.
	const scope = effectScope();
	// The record as a rule reads it; cheap (one pass over the main fields), and it
	// changes on every main-field edit. What a TABLE sees of it is `parentFor`.
	const parentDoc = computed(() => draftParentDoc(getPanel()));
	const tables = new WeakMap(); // raw table -> { saved, parent, rows: WeakMap }
	// The tables of the open panel, each with its own scope. When the panel is replaced
	// or closed their watchers stop and their entries go, so an old panel's rows are
	// not kept and its watchers do not run on later keystrokes.
	const live = new Map(); // raw table -> its scope
	scope.run(() =>
		watch(
			getPanel,
			() => {
				for (const [key, ts] of live) {
					ts.stop();
					tables.delete(key);
				}
				live.clear();
			},
			{ flush: "sync" }
		)
	);

	function tableCtx(t) {
		const key = toRaw(t);
		let c = tables.get(key);
		if (c) return c;
		const tscope = scope.run(() => effectScope());
		live.set(key, tscope);
		const names = computed(() => ruleParentKeys(t));
		// Just what this table's rules read of the record: a new object only when one
		// of those values changed (compared as JSON).
		const parent = shallowRef({});
		let sig = null;
		tscope.run(() =>
			watchEffect(
				() => {
					const all = parentDoc.value;
					const want = names.value;
					const doc = {};
					for (const k of want
						? [...want, "doctype", "name", "docstatus"]
						: Object.keys(all))
						doc[k] = all[k];
					const now = JSON.stringify(doc);
					if (now === sig) return;
					sig = now;
					parent.value = doc;
				},
				{ flush: "sync" }
			)
		);
		c = {
			// The table's saved rows by name; changes only with origJson.
			saved: computed(() => savedRowsByName(t)),
			// Each row's current place, for rules that read doc.idx; changes only when
			// rows are added, removed or moved.
			readsIdx: computed(() => ruleReadsIdx(t)),
			order: computed(() => new Map(t.rows.map((r, i) => [toRaw(r), i]))),
			parent,
			rows: new WeakMap(),
		};
		tables.set(key, c);
		return c;
	}

	// The record values a table's rules read (hand it to rowStatus / the row form).
	function parentFor(t) {
		return tableCtx(t).parent.value;
	}

	// The row's status. A rule's `doc.idx` is the row's current place (a dependency,
	// read only when some rule of the table reads it); `ri` is the fallback.
	function rowStatusFor(t, r, ri) {
		const c = tableCtx(t);
		const key = toRaw(r);
		let s = c.rows.get(key);
		if (!s) {
			s = computed((old) => {
				const savedRow = (r.__name && c.saved.value.get(r.__name)) || null;
				const index = c.readsIdx.value ? c.order.value.get(key) ?? ri : ri;
				const n = {
					...rowStatus(t, r, c.parent.value, savedRow, { index }),
					savedRow,
				};
				return sameStatus(n, old) ? old : n;
			});
			c.rows.set(key, s);
		}
		return s.value;
	}

	// The line above a table's grid (draftTables.changedSummary).
	const summaries = new WeakMap();
	function summaryFor(t) {
		const key = toRaw(t);
		let s = summaries.get(key);
		if (!s) {
			s = computed(() =>
				changedSummary(
					t,
					t.rows.map((r, ri) => {
						const st = rowStatusFor(t, r, ri);
						return st.savedRow ? st.changedFields : null;
					})
				)
			);
			summaries.set(key, s);
		}
		return s.value;
	}

	// One line per table, for the card's template.
	const summariesList = computed((old) => {
		const p = getPanel();
		const n = p ? p.tables.map(summaryFor) : [];
		return old && old.length === n.length && n.every((x, i) => x === old[i]) ? old : n;
	});

	return { rowStatusFor, parentFor, summaries: summariesList, dispose: () => scope.stop() };
}
