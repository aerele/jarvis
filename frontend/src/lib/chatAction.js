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
 * Anything unreadable (a tables value that is not a list of {fieldname, rows},
 * a fields item that is not an object) sets `unreadable: true` rather than being
 * dropped quietly, so the client shows its error view: an action we cannot
 * render must fail, never render a partial card.
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
	if (typeof a.verb === "string") a.verb = a.verb.trim().toLowerCase();
	if (a.fields !== undefined && a.fields !== null) normaliseFields(a);
	if (a.tables !== undefined && a.tables !== null) normaliseTables(a);
	return a;
}

const isPlainObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// Anything we cannot read is flagged, never dropped quietly: the client shows
// its error view instead of a card that hides part of what Create would send.
// The bad value is removed so the field stays an array a consumer can iterate.
function normaliseFields(a) {
	if (isPlainObject(a.fields)) {
		a.fields = Object.entries(a.fields).map(([label, value]) => ({ label, value }));
	} else if (!Array.isArray(a.fields)) {
		a.fields = [];
		a.unreadable = true;
	}
	const good = a.fields.filter(isPlainObject);
	if (good.length !== a.fields.length) a.unreadable = true;
	a.fields = good;
}

function normaliseTables(a) {
	if (isPlainObject(a.tables)) {
		// {items:{rows:[...]}} and {items:[...]} both mean the same thing as the
		// canonical [{fieldname:"items", rows:[...]}].
		a.tables = Object.entries(a.tables).map(([fieldname, spec]) => ({
			fieldname,
			rows: Array.isArray(spec) ? spec : spec && spec.rows,
		}));
	} else if (!Array.isArray(a.tables)) {
		a.tables = [];
		a.unreadable = true;
	}
	const good = a.tables.filter(
		(t) => isPlainObject(t) && typeof t.fieldname === "string" && Array.isArray(t.rows)
	);
	if (good.length !== a.tables.length) a.unreadable = true;
	a.tables = good;
}

const CLOSED_ACTION_RE = /```jarvis-action[ \t]*\n([\s\S]*?)```/;

/**
 * Tells a closed jarvis-action fence whose body is not a JSON object ("bad")
 * from no closed fence at all ("none", including one still streaming in).
 * @param {string} content
 * @returns {"none"|"ok"|"bad"}
 */
export function actionBlockState(content) {
	if (!content || !content.includes("jarvis-action")) return "none";
	const mt = content.match(CLOSED_ACTION_RE);
	if (!mt) return "none";
	try {
		const a = JSON.parse(mt[1].trim());
		return isPlainObject(a) ? "ok" : "bad";
	} catch {
		return "bad";
	}
}

/** Verbs whose real confirmation card is built by the server and arrives separately. */
export const GATED_VERBS = new Set(["submit", "cancel", "delete", "amend"]);

/**
 * The red error-card copy for a draft block the desktop cannot show, or "".
 * Error when the closed fence is not a JSON object, the action has unreadable
 * fields or tables, or a non-email action names a verb other than create / update.
 * A missing verb is not an error (the desktop treats it as create).
 * @param {string} content the assistant message text
 * @param {object|null} action the normalised action, or null
 * @param {string} agentName
 * @returns {string}
 */
export function draftBlockError(content, action, agentName) {
	const bad =
		actionBlockState(content) === "bad" ||
		(!!action &&
			(action.unreadable === true ||
				(action.kind !== "email" &&
					!!action.verb &&
					action.verb !== "create" &&
					action.verb !== "update" &&
					!GATED_VERBS.has(action.verb))));
	return bad ? `Couldn't show this draft. Ask ${agentName} to send it again.` : "";
}
