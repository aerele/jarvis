// What the draft card draws for a parsed ```jarvis-action``` block: an email, a
// write, or an error. Never a throw, so the card is never blank. Relative imports
// only, so bare `node --test` can run this file.
import { cardTableSummary } from "./cardTables.js";
import { GATED_VERBS } from "../../../frontend/src/lib/chatAction.js";

const errorView = (agentName) => ({
	kind: "error",
	message: `Couldn't show this draft. Ask ${agentName} to send it again.`,
});

function buildView(action, agentName) {
	if (!action || typeof action !== "object" || Array.isArray(action)) return errorView(agentName);
	// Normalising already removed what it could not read: that is an error, never
	// a card built from what is left.
	if (action.unreadable) return errorView(agentName);
	if (action.kind === "email")
		return { kind: "email", to: action.to, subject: action.subject, body: action.body };

	const verb = action.verb ? String(action.verb).trim().toLowerCase() : "create";
	// The server builds the real confirmation card for these; draw nothing here.
	if (GATED_VERBS.has(verb)) return { kind: "none" };
	if (verb !== "create" && verb !== "update") return errorView(agentName);

	const fields = (action.fields || []).map((f) => ({ label: f.label, value: f.value }));
	const tableLines = cardTableSummary(action);
	let invalid = "";
	if (Array.isArray(action.docs))
		invalid = `This draft carries a \`docs\` batch, which is a create_doc payload rather than a card. Ask ${agentName} to apply them as a batch.`;
	else if (verb === "create" && !fields.length && !tableLines.length)
		invalid = "This draft has no fields to show.";

	return {
		kind: "write",
		verb,
		heading: action.title || `${verb === "create" ? "Create" : "Update"} ${action.doctype || "record"}`,
		doctype: action.doctype,
		fields,
		tableLines,
		invalid,
	};
}

export function actionCardView(action, agentName) {
	try {
		return buildView(action, agentName);
	} catch {
		return errorView(agentName);
	}
}
