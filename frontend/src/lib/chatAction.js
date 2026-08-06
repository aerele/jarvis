/**
 * Canonicalises a ```jarvis-action block into the one shape the summary card and
 * the draft-model builder both expect.
 *
 * The spec (skills/jarvis-drafting/SKILL.md) is:
 *
 *     {kind:"doc", verb, doctype, title, summary?, fields:[{label,value}], tables?}
 *
 * Models drift from it in two ways that used to strand the card at
 * "Preparing summary..." with no error, no timeout and no retry:
 *
 *   1. `kind` omitted. The card renders on isEditVerb(), which never inspected
 *      kind, while the build watcher required kind === "doc". The card drew and
 *      its model was never built.
 *   2. `fields` emitted as an object ({fieldname: value}) rather than the
 *      [{label,value}] array the builder walks with for..of.
 *   3. `tables` emitted as an object ({items:{rows:[...]}} or {items:[...]})
 *      rather than the [{fieldname,rows}] array. This one THREW rather than
 *      hung: `for (const t of a.tables)` on a plain object is a TypeError, so
 *      the card showed "Could not load this draft. Tell me to try again."
 *
 * All are absorbed rather than rejected: the data is unambiguous, and the field
 * keys resolve either way because the builder matches on fieldname OR label.
 * Anything still unbuildable is left alone so it reaches the builder and raises
 * the honest error card, which is the rule that module already states: an action
 * we cannot render must fail, never render empty.
 *
 * Pure and dependency-free so it is unit-testable without mounting the view.
 */

/**
 * @param {unknown} a parsed JSON from a jarvis-action fence
 * @returns {object|null} the same object, canonicalised in place, or null
 */
export function normaliseAction(a) {
	if (!a || typeof a !== "object" || Array.isArray(a)) return null;
	// `kind` only distinguishes an email block from a doc one. A doc block is the
	// default, and the card's own render gate has always treated a missing kind as
	// one, so infer it rather than letting the two gates disagree.
	if (!a.kind && a.doctype) a.kind = "doc";
	if (a.fields && !Array.isArray(a.fields) && typeof a.fields === "object") {
		a.fields = Object.entries(a.fields).map(([label, value]) => ({ label, value }));
	}
	// {items:{rows:[...]}} and {items:[...]} both mean the same thing as the
	// canonical [{fieldname:"items", rows:[...]}]. A table whose rows cannot be
	// read as a list is dropped rather than guessed at: a half-populated grid the
	// user confirms believing it is the full set is worse than an empty one.
	if (a.tables && !Array.isArray(a.tables) && typeof a.tables === "object") {
		a.tables = Object.entries(a.tables)
			.map(([fieldname, spec]) => {
				const rows = Array.isArray(spec) ? spec : spec && spec.rows;
				return Array.isArray(rows) ? { fieldname, rows } : null;
			})
			.filter(Boolean);
	}
	return a;
}
