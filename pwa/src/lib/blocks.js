// The agent embeds structured payloads in ```jarvis-*``` fences. They are part
// of the reply's raw text, so a surface that renders the content verbatim shows
// the user a wall of JSON. Every other surface strips them and renders the ones
// it supports — the desktop SPA (ChatView) and the native app
// (components/chat/messages.tsx) both do this, with these same regexes; the PWA
// was the only one that didn't, which is why record lists and charts arrived as
// raw code blocks.
//
// Keep the STRIP list a superset of what we render: an unrendered block must
// still disappear from the prose rather than leak.

const CARDS_RE = /```jarvis-cards[ \t]*\n([\s\S]*?)```/;
const SKILL_RE = /```jarvis-skill[ \t]*\n([\s\S]*?)```/;
const CHART_RE = /```jarvis-chart[ \t]*\n([\s\S]*?)```/g;
const XY_RE = /```mermaid[ \t]*\n([\s\S]*?)```/g;

const STRIP_RES = [
	/```jarvis-action[ \t]*\n[\s\S]*?```/g,
	/```confirm[ \t]*\n[\s\S]*?```/g,
	/```jarvis-ask[ \t]*\n[\s\S]*?```/g,
	/```jarvis-cards[ \t]*\n[\s\S]*?```/g,
	/```jarvis-skill[ \t]*\n[\s\S]*?```/g,
	/```jarvis-macro[ \t]*\n[\s\S]*?```/g,
	/```jarvis-chart[ \t]*\n[\s\S]*?```/g,
	/```mermaid[ \t]*\n[ \t]*xychart-beta[\s\S]*?```/g,
	// A mermaid block that is NOT xychart-beta is a DIAGRAM (flowchart etc.) the
	// PWA can't draw. Strip it so it doesn't leak as a raw code block; the chip
	// from countDiagrams opens it in the web chat instead.
	/```mermaid[ \t]*\n(?![ \t]*xychart-beta)[\s\S]*?```/g,
];

// Separate object (not shared with STRIP_RES) so its /g lastIndex can't collide.
const DIAGRAM_RE = /```mermaid[ \t]*\n(?![ \t]*xychart-beta)[\s\S]*?```/g;

/** The agent's prose, with every control block removed. */
export function stripAgentBlocks(text) {
	let t = text || "";
	for (const re of STRIP_RES) t = t.replace(re, "");
	return t.replace(/\n{3,}/g, "\n\n").trim();
}

/** How many mermaid DIAGRAMS (flowcharts etc., not data charts) the reply holds. */
export function countDiagrams(text) {
	if (!text || !text.includes("```mermaid")) return 0;
	const m = text.match(DIAGRAM_RE);
	return m ? m.length : 0;
}

/** ```jarvis-cards``` → {title, cards:[{title, subtitle, doctype, name, fields}]}. */
export function parseCards(content) {
	if (!content || !content.includes("jarvis-cards")) return null;
	const mt = content.match(CARDS_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		const list = Array.isArray(a) ? a : a?.cards;
		if (!Array.isArray(list)) return null;
		const cards = list
			.slice(0, 60)
			.map((c) => ({
				title: String(c.title || c.name || "").trim(),
				subtitle: String(c.subtitle || "").trim(),
				doctype: String(c.doctype || "").trim(),
				name: String(c.name || "").trim(),
				fields: Array.isArray(c.fields)
					? c.fields.slice(0, 12).map((f) => ({
							label: String(f.label || ""),
							value: String(f.value != null ? f.value : ""),
					  }))
					: [],
			}))
			.filter((c) => c.title || c.fields.length);
		if (!cards.length) return null;
		return { title: String(a?.title || ""), cards };
	} catch {
		// Mid-stream the JSON is still being typed out — render nothing until it parses.
		return null;
	}
}

const ACTION_RE = /```jarvis-action[ \t]*\n([\s\S]*?)```/;

/**
 * ```jarvis-action``` → the document the agent is PROPOSING to write.
 *
 * This is the other half of the write story. For create/update the agent does
 * not call a tool at all: it emits this card and waits for a human to apply it.
 * Strip it without rendering it (which is what this surface used to do) and the
 * agent can describe the record it wants to make while the user has no button
 * to make it — the write is simply unreachable from the phone.
 *
 * Shape: {kind:"doc", verb, doctype, name?, title?, fields:[{label,value}]}
 * or    {kind:"email", to, subject, body}.
 */
export function parseAction(content) {
	if (!content || !content.includes("jarvis-action")) return null;
	const mt = content.match(ACTION_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		if (!a || typeof a !== "object") return null;
		if (Array.isArray(a.fields)) {
			a.fields = a.fields.map((f) => ({
				label: String(f.label || ""),
				value: f.value == null ? "" : String(f.value),
			}));
		}
		return a;
	} catch {
		// Mid-stream the JSON is still arriving — render nothing until it parses.
		return null;
	}
}

/** ```jarvis-skill``` → the skill names that shaped this reply. */
export function parseSkillsUsed(content) {
	if (!content || !content.includes("jarvis-skill")) return [];
	const mt = content.match(SKILL_RE);
	if (!mt) return [];
	return [
		...new Set(
			mt[1]
				.split(/[\n,]+/)
				.map((s) => s.trim().replace(/^[-*]\s*/, ""))
				.filter(Boolean)
				.map((s) => s.replace(/^custom-/, ""))
		),
	].slice(0, 6);
}

const TYPES = new Set([
	"bar",
	"line",
	"area",
	"pie",
	"donut",
	"scatter",
	"bubble",
	"heatmap",
	"boxplot",
	"radar",
	"funnel",
	"gauge",
]);

/** Minimal mermaid xychart-beta → chart spec (same subset the SPA understands). */
function parseXychart(body) {
	const text = String(body || "");
	if (!/^\s*xychart-beta\b/.test(text)) return null;
	const split = (s) => {
		const out = [];
		const re = /"([^"]*)"|'([^']*)'|([^,]+)/g;
		let m;
		while ((m = re.exec(s))) {
			const v = (m[1] ?? m[2] ?? m[3] ?? "").trim().replace(/^["']|["']$/g, "");
			if (v) out.push(v);
		}
		return out;
	};
	const tM = text.match(/title[ \t]+"([^"]*)"/);
	const xM = text.match(/x-axis[ \t]+\[([^\]]*)\]/);
	const x = xM ? split(xM[1]) : [];
	const series = [];
	let anyBar = false;
	const re = /\b(bar|line)[ \t]+(?:"([^"]*)"[ \t]+)?\[([^\]]*)\]/g;
	let m;
	while ((m = re.exec(text))) {
		const data = split(m[3])
			.map(Number)
			.filter((n) => !Number.isNaN(n));
		if (!data.length) continue;
		if (m[1] === "bar") anyBar = true;
		series.push({ name: m[2] || "Value", data });
	}
	if (!series.length) return null;
	const spec = { type: anyBar ? "bar" : "line", x, series };
	if (tM) spec.title = tM[1].trim();
	return spec;
}

/** Every chart spec in a message: ```jarvis-chart``` blocks + mermaid xycharts. */
export function parseCharts(content) {
	if (!content || (!content.includes("jarvis-chart") && !content.includes("xychart-beta")))
		return [];
	const specs = [];
	for (const mt of content.matchAll(CHART_RE)) {
		try {
			const s = JSON.parse(mt[1].trim());
			if (s && typeof s === "object" && TYPES.has(s.type) && Array.isArray(s.series))
				specs.push(s);
		} catch {
			/* incomplete mid-stream JSON */
		}
	}
	for (const mt of content.matchAll(XY_RE)) {
		const s = parseXychart(mt[1]);
		if (s) specs.push(s);
	}
	return specs;
}

/** A tool row's terminal state. The backend writes free-text statuses, so match
 * loosely rather than enumerating them. */
export function toolStatus(raw) {
	const s = String(raw || "").toLowerCase();
	if (/err|fail/.test(s)) return "error";
	if (/run|start|progress|pending/.test(s)) return "running";
	return "done";
}
