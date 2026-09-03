/**
 * jarvis#1062 P0-2 (production-readiness audit): findings/coverage text is
 * bundle-generated prose (evaluator output, not authored copy) - it cannot be
 * rewritten wholesale, but obvious machine tokens can be stripped/relocated
 * where doing so is unambiguous and safe:
 *
 *   - a rule code ("nsv-grad-7d92", "nsv-tieout-7d92" - lowercase, hyphenated,
 *     ending in an alnum id) read out of the sentence, not just the
 *     structured `rule_id` field FindingsPanel.vue already has separately.
 *   - an evaluation flag written as a parenthetical boolean,
 *     e.g. "(clears reorder gate: False)".
 *   - a DocType.field schema reference, e.g. "Warehouse.account".
 *   - the literal token `not_evaluable` -> "not evaluable" wherever it
 *     survives in running prose (the class-count case above already lifts
 *     the common "N class(es) not_evaluable" phrasing out entirely; this is
 *     a fallback for any other appearance).
 *
 * Conservative on purpose: unmatched text is returned untouched. A markdown
 * link's `(url)` is never mistaken for a flag - the boolean-flag pattern
 * requires a literal ": True"/": False" inside the parens, which a URL never
 * contains - and a capitalised URL host inside a markdown link's target
 * (e.g. "Example.com") is never mistaken for a DocType.field reference; see
 * DOCTYPE_FIELD_RE below.
 */

// Rule ids in this codebase have an exact shape: <prefix>-<name>-<hex4>,
// e.g. "nsv-grad-7d92", "nsv-tieout-7d92" (see jarvis-agents/agents/*/rules.
// ids.json - the last segment is always exactly 4 lowercase hex chars, minted
// as a truncated HMAC). Matching on "last segment has a digit" was too loose:
// it also caught ordinary hyphenated prose ending in a number, e.g. an
// "audit-run-2026" style phrase, and deleted it from the sentence. A bare
// 4-char hex requirement alone still isn't enough - "2026" is itself valid
// hex (all digits 0-9 are hex chars) - so the callback below additionally
// requires the final segment to contain at least one a-f LETTER, matching
// every id actually observed in rules.ids.json (7d92, 4b1c, 2a6f, e058) and
// excluding plain 4-digit numbers like years.
const RULE_CODE_RE = /\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+-([0-9a-f]{4}))\b/g;
const FLAG_RE = /\(([A-Za-z][\w\s]*:\s*(?:True|False))\)/g;
const CLASS_COUNT_RE = /,?\s*with\s+(\d+)\s+class\(es\)\s+not_evaluable\b/gi;
// DocType names are Title Case, one word ("Warehouse") in every example
// observed in bundle prose; the field after the dot is always lower
// snake_case. A multiword allowance was tried (per review suggestion, for
// "Stock Ledger Entry.account") and reverted: bundle prose is imperative and
// sentence-initial-capitalised constantly ("Configure Warehouse.account
// first."), and there is no syntactic way to tell a real multiword DocType
// name from an ordinary capitalised sentence-starter sitting in front of a
// one-word reference - "Configure Warehouse.account" is indistinguishable
// from "Stock Ledger Entry.account" by shape alone, and the former is far
// more common here. Single-word only, so it never reaches backwards past a
// word that isn't actually part of the reference.
//
// Review finding: a bare "[A-Z][A-Za-z]+\.[a-z]+" also matches a capitalised
// URL host sitting inside a markdown link's target, e.g. "Example.com" in
// "[the policy](https://Example.com/x)" - stripping it mangled the link.
// Guarded on both sides against exactly that shape: not preceded by "//" (a
// URL scheme/host) or "](" (a markdown link's target opening), and not
// immediately followed by "/" (the rest of that URL) or ")" (the link's
// closing paren) - a real DocType.field reference in running prose never
// borders a URL like that. (The trailing guard's cost: "(Warehouse.account)"
// with no space before the ")" is no longer extracted either - the same
// heuristic the review asked for, applied evenly.)
const DOCTYPE_FIELD_RE = /(?<!\/\/|\]\()\b([A-Z][A-Za-z]+\.[a-z][a-z_]*)\b(?![/)])/g;
const RAW_TOKEN_RE = /\bnot_evaluable\b/g;

/**
 * Pulls the machine-shaped pieces of `text` out into a flat, de-duplicated
 * list of labelled technical tokens, returning the REMAINING prose separately
 * ({ text, details }). `details` is `[{label, value}]`, always monospace-safe
 * plain strings - render it inside a "Technical details" block, never inline.
 */
export function extractTechnicalDetails(raw) {
	let text = String(raw || "");
	const details = [];
	const seen = new Set();

	function add(label, value) {
		const key = label + ":" + value;
		if (seen.has(key)) return;
		seen.add(key);
		details.push({ label, value });
	}

	text = text.replace(CLASS_COUNT_RE, (_, n) => {
		add("Not evaluable", `${n} class${n === "1" ? "" : "es"}`);
		return "";
	});
	text = text.replace(FLAG_RE, (_, flag) => {
		add("Flag", flag);
		return "";
	});
	text = text.replace(DOCTYPE_FIELD_RE, (m) => {
		add("Field reference", m);
		return "";
	});
	text = text.replace(RULE_CODE_RE, (m, _full, hex4) => {
		if (!/[a-f]/.test(hex4)) return m; // e.g. "2026" - all-digit, not a real id suffix
		add("Rule", m);
		return "";
	});
	text = text.replace(RAW_TOKEN_RE, "not evaluable");
	// collapse whitespace/punctuation debris left by the removals above -
	// double spaces, a doubled ": :" where a rule code sat between two
	// colons, a paren left with only leading/trailing space inside it once
	// its own DocType.field content was pulled out ("(Warehouse.account
	// empty)" -> "( empty)"), orphaned leading/trailing punctuation. This is
	// tidy-up, not a grammar guarantee - arbitrary bundle prose run through
	// a removal pass can still read slightly awkwardly; it is never left
	// worse than the raw machine token it replaced.
	text = text
		.replace(/\(\s*\)/g, "")
		.replace(/\(\s+/g, "(")
		.replace(/\s+\)/g, ")")
		.replace(/([,.;:])\s*\1+/g, "$1")
		.replace(/\s{2,}/g, " ")
		.replace(/\s+([,.;:])/g, "$1")
		.replace(/^[\s,:;-]+/, "")
		.replace(/[\s,:;-]+$/, "")
		.trim();

	return { text, details };
}
