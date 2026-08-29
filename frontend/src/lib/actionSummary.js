// frontend/src/lib/actionSummary.js
// Pure display helpers for the summary-first confirmation card. No Vue, no DOM.
// Summarization is MODEL-DRIVEN: the card renders the fields the model proposed and
// an optional model-written headline. It imposes no opinion on which fields matter
// or what to total - that is the model's job, since it knows the doctype.

export function proposedFields(action) {
	return (action.fields || [])
		.filter((f) => String(f.value ?? "").trim() !== "")
		.map((f) => ({ label: f.label, value: f.value }));
}

export function changedFields(model) {
	return model.fields
		.filter((f) => f.changed)
		.map((f) => ({ label: f.label, from: f.orig ?? "", to: f.value ?? "" }));
}

export function lineItemSummary(table) {
	return {
		fieldname: table.fieldname,
		label: table.label,
		count: table.rows.length,
		columns: table.columns.map((c) => c.label),
		rows: table.rows.map((r) => ({ cells: table.columns.map((c) => r[c.fieldname] ?? "") })),
	};
}

export function summarize(model, action = {}) {
	const headline = String(action.summary ?? "").trim();
	const tables = (model.tables || []).filter((t) => (t.rows || []).length).map(lineItemSummary);
	if (model.verb === "update") {
		return { kind: "update", headline, diff: changedFields(model), tables };
	}
	return { kind: "create", headline, rows: proposedFields(action), tables };
}

// A create_docs batch parks one card. Its dry-run preview carries the created
// records ({doctype, name}). Turn that into the flat action lines the pending card
// renders as bullets. Pure; returns null when the preview is not a batch (a
// single-doc dry-run, or a described-intent card).
//
// ``would.notes`` is deliberately NOT returned. It is a tool argument the MODEL
// writes (jarvis/tools/create_doc.py), and this is the rendering that runs when the
// server built no card - including when build_card FAILS. Rendered here as
// unattributed bullets it reads as system truth, letting a prompt-injected or simply
// confused agent caption its own confirmation ("these already exist - confirming
// changes nothing") above a write that inserts 20 rows. The card is the human's
// INDEPENDENT check on the agent; escaped rendering stops XSS, not lying. The agent
// can still say what it likes in chat, where the claim is attributed to it.
export function batchFromPreview(preview) {
	const would = preview && preview.would;
	if (!would || typeof would !== "object" || !Array.isArray(would.created)) return null;
	return {
		actions: would.created.map((d) => ({ doctype: d.doctype, name: d.name })),
	};
}

// ── Pending-confirmation card (F9/F15) ──────────────────────────────────────
// The write-safety gate attaches a render-ready ``card`` object to a parked
// write's preview (server side: jarvis/chat/confirm_card.py) plus a wall-clock
// ``expires_at``. These pure helpers pull the card out for the token confirmation
// card and compute the expiry, so the SPA renders "what will change" + a real
// expiry state instead of the raw dry-run JSON. A missing/unknown card -> null, and
// the card falls back to the summary + raw-preview rendering.

const CARD_KINDS = new Set([
	"create",
	"update",
	"bulk_update",
	"verb",
	"email",
	"method",
	"batch_create",
	"bulk_email",
	"share",
	"assign",
	"skill",
	"wiki",
	"import",
]);

// The structured card for a parked action ({kind, ...}), or null when the server
// built none (an older token, or an uncovered tool shape).
export function pendingCardOf(pa) {
	const card = pa && pa.preview && pa.preview.card;
	if (!card || typeof card !== "object" || !CARD_KINDS.has(card.kind)) return null;
	return card;
}

// One-line sentence for a verb card (submit/cancel/delete/amend/apply): "Will
// submit this Sales Order SO-0001" / "Will cancel 6 Sales Orders".
export function verbSentence(card) {
	const verb = card.verb || "run";
	const dt = card.doctype || "record";
	const act = card.action ? ` "${card.action}" to` : "";
	if ((card.count || 1) > 1) {
		return `Will ${verb}${act} ${card.count} ${pluralize(dt, card.count)}`;
	}
	const name = (card.targets && card.targets[0]) || "";
	return `Will ${verb}${act} this ${dt}${name ? " " + name : ""}`;
}

// Wall-clock expiry for a parked confirmation. ``expiresAt`` is epoch SECONDS (as
// the server stamps it); ``nowMs`` is Date.now(). Returns {expired, secondsLeft}
// (secondsLeft null when there is no expiry stamp — an older token).
export function pendingExpiry(expiresAt, nowMs) {
	if (typeof expiresAt !== "number" || !expiresAt) return { expired: false, secondsLeft: null };
	const secondsLeft = Math.round(expiresAt - nowMs / 1000);
	return { expired: secondsLeft <= 0, secondsLeft };
}

// ── Post-action receipt chips ───────────────────────────────────────────────
// A gated write, once the user clicks Confirm or Discard, is replaced by a
// DURABLE receipt chip instead of the card vanishing. These pure helpers turn
// the tool + args + structured result into the chip's one-liner + target links,
// for all three outcomes (confirmed / discarded / failed), single and bulk. The
// verb table + result shapes mirror jarvis/tools/*.py and api._describe_call.

const RECEIPT_VERB = {
	submit_doc: { past: "Submitted", present: "submit" },
	cancel_doc: { past: "Cancelled", present: "cancel" },
	delete_doc: { past: "Deleted", present: "delete" },
	update_doc: { past: "Updated", present: "update" },
	create_doc: { past: "Created", present: "create" },
	create_docs: { past: "Created", present: "create" },
	amend_doc: { past: "Amended", present: "amend" },
	apply_workflow_action: { past: "Applied", present: "apply" },
	send_email: { past: "Emailed", present: "email" },
	// The import is only QUEUED at confirm (form_start_import kicks off a background
	// job) - "Ran"/"Created" would falsely claim it already finished (Slice A ends at
	// the queue; the completion tell is Slice B).
	run_import: { past: "Queued", present: "queue" },
};

function pluralize(word, n) {
	if (n === 1 || !word) return word;
	return /[^aeiou]y$/i.test(word) ? word.slice(0, -1) + "ies" : word + "s";
}

export function docUrl(doctype, name) {
	if (!doctype || !name) return "";
	return `/app/${String(doctype).toLowerCase().replace(/ /g, "-")}/${encodeURIComponent(name)}`;
}

// How many records the call targeted, read from the args shape (a bulk arg is a
// non-empty list; anything else is a single call).
function argCount(args) {
	for (const k of ["names", "updates", "docs", "messages"]) {
		if (Array.isArray(args[k])) return args[k].length;
	}
	// A single send_email to several addresses: count the recipients so a
	// discarded/failed one-email chip reads "3 recipients", not "1".
	if (Array.isArray(args.recipients)) return args.recipients.length;
	return 1;
}

// The affected record names: from the structured result for a real execution,
// else from the args (discarded — nothing ran; or a failed write whose {ok:false}
// envelope carried no names).
function receiptNames(tool, args, data, outcome) {
	if (outcome !== "discarded") {
		for (const k of ["submitted", "cancelled", "updated", "deleted"]) {
			if (Array.isArray(data[k])) return data[k].slice();
		}
		if (Array.isArray(data.created))
			return data.created.map((d) => d && d.name).filter(Boolean);
		if (Array.isArray(data.amended))
			return data.amended.map((d) => d && d.name).filter(Boolean);
		if (Array.isArray(data.results))
			return data.results.map((d) => d && d.name).filter(Boolean);
		if (Array.isArray(data.sent))
			return data.sent.map((d) => (d && (d.recipients || d.name)) || "").filter(Boolean);
		if (tool === "send_email" && data.recipients) return [].concat(data.recipients);
		if (data.name) return [data.name];
		if (outcome === "confirmed" || outcome === "auto_applied") return [];
	}
	if (Array.isArray(args.names)) return args.names.slice();
	if (Array.isArray(args.updates)) return args.updates.map((u) => u && u.name).filter(Boolean);
	if (Array.isArray(args.docs)) return args.docs.map((d) => d && d.name).filter(Boolean);
	if (Array.isArray(args.messages))
		return args.messages.map((m) => (m && (m.recipients || m.name)) || "").filter(Boolean);
	if (args.name) return [args.name];
	if (args.recipients) return [].concat(args.recipients);
	return [];
}

// The render-ready receipt: { outcome, icon, tone, title, subject, doctype,
// action, count, targets:[{name,url}], error }.
export function receiptView(tool, args, result, outcome) {
	args = args || {};
	const data = (result && typeof result === "object" && result.data) || {};
	const verb = RECEIPT_VERB[tool] || { past: "Ran", present: "run" };
	const isEmail = tool === "send_email";
	const names = receiptNames(tool, args, data, outcome);
	const doctype =
		data.doctype ||
		args.doctype ||
		(Array.isArray(data.created) && data.created[0] && data.created[0].doctype) ||
		(Array.isArray(args.docs) && args.docs[0] && args.docs[0].doctype) ||
		"record";
	const action = args.action || data.action || "";
	// auto_applied (an armed macro ran it uncarded) is a real execution, like confirmed.
	const ran = outcome === "confirmed" || outcome === "auto_applied";
	const count =
		ran && typeof data.count === "number"
			? data.count
			: ran
			? names.length || argCount(args)
			: argCount(args);

	// Target links (create keeps each row's own doctype; email targets have no doc).
	// A DELETED record gets no link: the row is gone, so the shortcut would open a
	// 404. Only when it actually ran, though - a discarded or failed delete left the
	// record alive, and that one is worth opening.
	const isGoneDelete = tool === "delete_doc" && outcome === "confirmed";
	let targets;
	if (Array.isArray(data.created)) {
		targets = data.created
			.filter((d) => d && d.name)
			.map((d) => ({ name: d.name, url: docUrl(d.doctype || doctype, d.name) }));
	} else if (isEmail || isGoneDelete) {
		targets = names.map((n) => ({ name: n, url: "" }));
	} else {
		targets = names.map((n) => ({ name: n, url: docUrl(doctype, n) }));
	}

	let subject;
	if (isEmail) {
		subject = `${count} ${count === 1 ? "recipient" : "recipients"}`;
	} else if (count === 1 && names.length === 1) {
		subject = `${doctype} ${names[0]}`;
	} else if (count === 1) {
		subject = `a ${doctype}`;
	} else {
		subject = `${count} ${pluralize(doctype, count)}`;
	}
	const wfPrefix = tool === "apply_workflow_action" && action ? `'${action}' to ` : "";

	let icon, tone, title;
	let error = "";
	if (outcome === "discarded") {
		icon = "discarded";
		tone = "muted";
		title = `Discarded — did not ${verb.present} ${wfPrefix}${subject}`;
	} else if (outcome === "failed") {
		icon = "failed";
		tone = "danger";
		error = (result && result.error && result.error.message) || "";
		title =
			count > 1
				? `Failed — none of the ${count} ${pluralize(
						doctype,
						count
				  )} were ${verb.past.toLowerCase()}`
				: `Failed — ${subject} was not ${verb.past.toLowerCase()}`;
	} else if (outcome === "auto_applied") {
		// An armed macro ran this write WITHOUT a confirmation card. Render a distinct
		// receipt (not an identical "confirmed" chip) so it never reads as a silent run.
		icon = "auto_applied";
		tone = "success";
		title = `${verb.past} ${wfPrefix}${subject} — automatically, no confirmation`;
	} else {
		icon = "confirmed";
		tone = "success";
		title = `${verb.past} ${wfPrefix}${subject}`;
	}
	return { outcome, icon, tone, title, subject, doctype, action, count, targets, error };
}

// Convenience one-liner (tests / plain-text contexts).
export function receiptText(tool, args, result, outcome) {
	return receiptView(tool, args, result, outcome).title;
}
