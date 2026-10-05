const OUTCOMES = {
	Settled: "No reminder needed",
	"Within Terms": "Not overdue",
	Hold: "Review needed before contact",
	"Reminder Candidate": "Reminder draft ready for review",
};

const INDIA_FIELDS = {
	billing_address_gstin: "Customer GSTIN",
	company_gstin: "Company GSTIN",
	einvoice_status: "E-invoice status",
	gst_category: "GST category",
	irn: "Invoice reference number (IRN)",
};

const DRAFT_MARKER = "UNSENT DRAFT — human verification required";
const OPENING_PREFIX = "Opening/prior-period invoice included. ";

function indiaContext(line) {
	const content = line.slice("India review: ".length);
	const boundary = content.search(
		/(?:billing_address_gstin|company_gstin|einvoice_status|gst_category|irn): /
	);
	if (boundary < 0) return { guidance: content, fields: [] };
	const fields = content
		.slice(boundary)
		.split("; ")
		.map((entry) => {
			const separator = entry.indexOf(": ");
			const key = entry.slice(0, separator);
			return { label: INDIA_FIELDS[key], value: entry.slice(separator + 2) };
		});
	if (
		fields.some((field) => !field.label || !field.value) ||
		new Set(fields.map((field) => field.label)).size !== fields.length
	) {
		return { guidance: content, fields: [] };
	}
	return { guidance: content.slice(0, boundary).trim(), fields };
}

export function arFindingPresentation(finding, agent) {
	if (
		(finding.agent || agent) !== "ar-collections-operator" ||
		finding.rule_id !== "ar-review-v1" ||
		finding.ref_doctype !== "Sales Invoice" ||
		finding.result_class !== "derived_candidate" ||
		typeof finding.detail_md !== "string"
	)
		return null;

	const lines = finding.detail_md.replace(/\r\n/g, "\n").trim().split("\n");
	const draftIndex = lines.indexOf(DRAFT_MARKER);
	const body = (draftIndex < 0 ? lines : lines.slice(0, draftIndex)).filter((line) =>
		line.trim()
	);
	if (body.length < 4 || body.length > 5) return null;
	const outcome = /^(Settled|Within Terms|Hold|Reminder Candidate) — (.+)$/.exec(body[0]);
	const reference = String(finding.ref_name || "")
		.replace(/\[/g, "(")
		.replace(/\]/g, ")")
		.replace(/\s+/g, " ")
		.trim()
		.slice(0, 200);
	const identity = /^Customer: (.+); account: (.+); company: (.+)\.$/.exec(body[1]);
	const scope = /^Cutoff (\d{4}-\d{2}-\d{2}); (.+)$/.exec(body[3]);
	if (
		!outcome ||
		outcome[2] !== reference ||
		!identity ||
		!scope ||
		body[1].split("; account: ").length !== 2 ||
		body[1].split("; company: ").length !== 2 ||
		(body[4] && !body[4].startsWith("India review: ")) ||
		draftIndex >= 0 !== (outcome[1] === "Reminder Candidate")
	)
		return null;

	const opening = body[2].startsWith(OPENING_PREFIX);
	const explanation = opening ? body[2].slice(OPENING_PREFIX.length) : body[2];
	const balance = /^Recorded balance at cutoff: (.+?)\. (.+)$/.exec(explanation);
	const reminderDraft =
		draftIndex < 0
			? ""
			: lines
					.slice(draftIndex + 1)
					.join("\n")
					.trim();
	if (draftIndex >= 0 && !reminderDraft) return null;

	return {
		outcome: OUTCOMES[outcome[1]],
		summary: balance ? balance[2] : explanation,
		balance: balance ? balance[1] : "",
		cutoff: scope[1],
		facts: [
			{ label: "Customer", value: identity[1] },
			{ label: "Receivable account", value: identity[2] },
			{ label: "Company", value: identity[3] },
		],
		opening,
		limitations: scope[2].split(/\. (?=[A-Z])/),
		india: body[4] ? indiaContext(body[4]) : null,
		reminderDraft,
	};
}
