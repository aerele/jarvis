// The draft form builder: turns an action (a typed `jarvis-action` block, or a server draft card
// adapted by lib/draftFromCard.js and marked FROM_CARD) into the model the edit form, the card
// and Preview show. Moved out of ChatView.vue unchanged so the phone builds the same models;
// draftModel.spec.js holds the models the old in-view code built and requires these.
//
// No Vue, no router, no store, no API module: every read of the server is INJECTED.
//   makeDraftModelBuilder({
//     getDoctypeFormMeta(doctype) -> {ok, fields, tables, title_field, is_submittable}
//     loadDocForEdit(doctype, name) -> {values, tables, docstatus}   (rejects when not readable)
//     ruleValues(doctype, table, links) -> {ok, values}
//   }) -> { buildDraftModel(action), fillRuleValues(doctype, tables, isCurrent), formMetaCache }
// One call makes one builder with its own form-meta cache (never invalidated, as before: the view
// made it once per mounted ChatView).
import {
	modelField,
	mainRuleBase,
	mainRuleDoc,
	ruleAdmits,
	ruleTableNames,
	ruleTables,
} from "./mainRules.js";
import {
	applyRuleValues,
	buildDraftTable,
	parentDefaults,
	ruleFetchLinks,
} from "./draftTables.js";
import { FROM_CARD, resolveDraftField } from "./toolDraftPanel.js";

// "Item Group" / "item_group" / "itemGroup" all → "itemgroup": the agent's
// action JSON labels fields sometimes by display label, sometimes by fieldname.
export function normKey(s) {
	return String(s || "")
		.toLowerCase()
		.replace(/[^a-z0-9]/g, "");
}
// Resolve one card label to a field: exact normalized match on label/fieldname,
// else unique containment (e.g. "UOM" → stock_uom when it's the only sensible
// hit; required fields win ambiguous shorthands).
export function actField(meta, label) {
	const k = normKey(label);
	if (!k) return null;
	if (meta.map[k]) return meta.map[k];
	const hits = meta.fields.filter((f) => f._kl.includes(k) || f._kf.includes(k));
	if (hits.length === 1) return hits[0];
	if (hits.length > 1) {
		const reqd = hits.filter((f) => f.reqd);
		if (reqd.length === 1) return reqd[0];
	}
	return null;
}

// How long a build waits for the rule values before it shows the card with the defaults.
export const RULE_VALUES_WAIT_MS = 1500;

export function makeDraftModelBuilder({ getDoctypeFormMeta, loadDocForEdit, ruleValues }) {
	const formMetaCache = {};

	async function formMeta(doctype) {
		if (formMetaCache[doctype]) return formMetaCache[doctype];
		const r = await getDoctypeFormMeta(doctype);
		if (!r || !r.ok) throw new Error("no form meta");
		for (const f of r.fields) {
			f._kl = normKey(f.label);
			f._kf = normKey(f.fieldname);
		}
		formMetaCache[doctype] = r;
		return r;
	}

	// Build the draft model from an action + form meta (+ live doc for updates),
	// WITHOUT opening the panel - so the editable panel and later summary/preview
	// surfaces can share one construction path.
	async function buildDraftModel(a) {
		if (!a || a.kind !== "doc" || !a.doctype) return;
		const verb = a.verb === "update" ? "update" : "create";
		// An action block we cannot render must FAIL, not render empty - a blank card
		// reads as "Jarvis did not try" rather than "Jarvis emitted a shape I cannot
		// draw". `docs` is a create_doc batch payload, not an action key (AGENTS.md:122
		// documents fields+tables for ONE record) - throw on it for EITHER verb: a
		// {"verb":"update","docs":[...]} fusion otherwise builds an empty diff, renders
		// "No field changes." with Confirm ENABLED, and sends update_doc(dt, "", {}).
		// A hybrid carrying BOTH fields and docs throws too: rendering the fields and
		// dropping the docs is a half-card the user confirms believing it is the set.
		if (Array.isArray(a.docs)) {
			const err = new Error(
				"This draft carries a `docs` batch, which is a create_doc payload rather than a card. Ask me to apply them as a batch."
			);
			err.jvUserMessage = err.message;
			throw err;
		}
		// Create-only: a fieldless UPDATE block legitimately renders "No field changes.",
		// and a tables-only create is legal, so neither may throw.
		if (verb === "create" && !(a.fields || []).length && !(a.tables || []).length) {
			const err = new Error("This draft has no fields to show.");
			err.jvUserMessage = err.message;
			throw err;
		}
		let meta;
		try {
			meta = await formMeta(a.doctype);
		} catch (e) {
			return;
		} // no meta → no panel (old card still shows)
		let base = { values: {}, tables: {} };
		let storedUnavailable = false;
		if (verb === "update" && a.name) {
			try {
				base = await loadDocForEdit(a.doctype, a.name);
			} catch (e) {
				// A server draft card's form (a[FROM_CARD], which a typed action cannot carry) shows the
				// draft's own cells and says so; the card itself already shows what changes.
				storedUnavailable = !!a[FROM_CARD];
				// Without the saved rows the card cannot show which rows a proposed table
				// keeps or removes, so never offer to apply one blind (CR-3).
				if ((a.tables || []).length && !a[FROM_CARD]) {
					const err = new Error(
						"Could not load this record's saved rows, so I can't show what would change. Tell me to try again."
					);
					err.jvUserMessage = err.message;
					throw err;
				}
				/* not editable → create-style view */
			}
		}
		// proposed main fields: agent's fields resolved by label-or-fieldname
		const metaLookup = { fields: meta.fields, map: {} };
		for (const f of meta.fields) {
			if (f._kf && !metaLookup.map[f._kf]) metaLookup.map[f._kf] = f;
			if (f._kl) metaLookup.map[f._kl] = f;
		}
		const proposed = {}; // fieldname -> value
		for (const f of a.fields || []) {
			// A server draft's key is the field its `fieldname` names, exactly; a typed action's
			// label is resolved as before (lib/toolDraftPanel resolveDraftField).
			const m = resolveDraftField(
				f,
				meta.fields,
				(label) => actField(metaLookup, label),
				!!a[FROM_CARD]
			);
			if (m && m.fieldtype !== "Table") proposed[m.fieldname] = f.value;
		}
		const aTables = {};
		for (const t of a.tables || []) if (t && t.fieldname) aTables[t.fieldname] = t.rows || [];
		const tables = [];
		for (const [tf, spec] of Object.entries(meta.tables || {})) {
			const metaField = meta.fields.find((f) => f.fieldname === tf) || { reqd: 0 };
			const table = buildDraftTable({
				fieldname: tf,
				spec,
				required: !!metaField.reqd,
				proposed: aTables[tf],
				saved: (base.tables || {})[tf] || [],
				verb,
			});
			if (table) tables.push(table);
		}
		const tableNames = ruleTableNames(meta.fields);
		const fields = [];
		const seen = new Set();
		// A field whose mandatory_depends_on holds, or cannot be told, is shown too (never a
		// read_only one): the rule reads the defaults, the saved record and the proposed values.
		const ruleBase = mainRuleBase(meta.fields, base.values, base.tables, tableNames);
		let ruleDoc = null;
		const ruleSays = (f) => {
			if (!ruleBase) return false;
			ruleDoc =
				ruleDoc ||
				mainRuleDoc(
					ruleBase,
					meta.fields.filter((m) => m.fieldname in proposed),
					proposed,
					ruleTables(tableNames, tables)
				);
			return ruleAdmits(f, ruleDoc, true);
		};
		for (const f of meta.fields) {
			if (f.fieldtype === "Table") continue;
			const has = f.fieldname in proposed;
			const baseV = base.values[f.fieldname];
			// Show: agent-proposed fields + required fields + (update) filled fields the agent referenced
			if (!has && !f.reqd && !ruleSays(f)) continue;
			// proposed: the agent set a value for this field (vs. shown only because reqd)
			const pf = modelField(f, has ? proposed[f.fieldname] : baseV, baseV, verb, {
				proposed: has,
				ruleAdded: !has && !f.reqd,
			});
			fields.push(pf);
			seen.add(f.fieldname);
		}
		// The fields a later edit may make required (see mainRules.revealRuleFields).
		const ruleFields = ruleBase
			? meta.fields
					.filter(
						(f) =>
							f.fieldtype !== "Table" &&
							!f.read_only &&
							f.mandatory_depends_on &&
							!seen.has(f.fieldname)
					)
					.map((f) => ({ field: f, value: base.values[f.fieldname] }))
			: [];
		// Bounded: the card shows with the defaults rather than wait on a slow answer.
		await Promise.race([
			fillRuleValues(a.doctype, tables),
			new Promise((resolve) => setTimeout(resolve, RULE_VALUES_WAIT_MS)),
		]);
		// A Prompt-autonamed DocType (e.g. Server Script) carries its document name as a
		// proposed "name" field, not a real DocField, so it never lands in `fields`.
		// Capture it here so applyDraft sends it as `name` (the backend folds it into
		// the create values). Field/series-named DocTypes have no such field -> "".
		const proposedCreateName =
			verb === "update"
				? ""
				: String(
						(
							(a.fields || []).find(
								(x) => String(x.label || "").toLowerCase() === "name"
							) || {}
						).value ?? ""
				  );
		return {
			verb,
			doctype: a.doctype,
			docName: verb === "update" ? a.name || "" : proposedCreateName,
			title: a.title || "",
			titleField: meta.title_field || "",
			submittable: !!meta.is_submittable,
			// Multi-step plans: the card marks non-final steps "continue": 1; the
			// bench then dispatches a follow-up agent turn after Apply so the agent
			// stages the next step without the user typing "continue".
			cont: a.continue ? 1 : 0,
			linkContext: { ...base.values, ...proposed },
			// For child rows' rules: Desk's new-form defaults and the record's docstatus.
			parentDefaults: verb === "create" ? parentDefaults(meta.fields) : {},
			docstatus: Number(base.docstatus || 0),
			fields,
			...(ruleBase ? { ruleBase } : {}),
			...(ruleFields.length ? { ruleFields } : {}),
			...(ruleBase && tableNames.length ? { ruleTables: tableNames } : {}),
			tables,
			tableMeta: meta.tables || {},
			applying: false,
			error: "",
			updatedToast: false,
			...(storedUnavailable ? { storedUnavailable: true } : {}),
		};
	}

	// The hidden values the rows' rules read that Desk fetches from a link (is_fixed_asset
	// from the Item), one call per table that still needs one, so the card shows and
	// requires what Desk would before the record is saved. A failure keeps the defaults;
	// `isCurrent` drops an answer that lands after its panel was closed or replaced.
	async function fillRuleValues(doctype, tables, isCurrent = () => true) {
		await Promise.all(
			(tables || []).map(async (t) => {
				const links = ruleFetchLinks(t);
				if (!Object.keys(links).length) return;
				try {
					const r = await ruleValues(doctype, t.fieldname, links);
					if (r && r.ok && isCurrent()) applyRuleValues(t, r.values, links);
				} catch (e) {
					// the rules read the defaults, as before
				}
			})
		);
	}

	return { buildDraftModel, fillRuleValues, formMetaCache };
}
