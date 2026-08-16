// The common "producing an artifact" live card (jarvis#884), replacing the
// dashboard-only card issue #874 shipped. Same idea, generalized: whichever
// write tool a main-chat turn is actually running names the artifact it is
// building, and the same four-phase tick mechanics dashboardBuildCard.js
// already proved out for dashboards now drive pdf/spreadsheet/image turns
// too — parameterized, not forked (see dashboardBuildPhase's toolSets arg).
//
// Deliberate exclusion: `run_report` is a DATA tool (dashboardBuildCard.js's
// own DATA_TOOLS already lists it) — it reads a saved Report's rows for
// ordinary Q&A ("what were last month's sales") and never by itself produces
// a downloadable artifact. A report the user actually wants AS A FILE goes
// through `report_pdf`/`export_document` (PDF) or `export_query` (workbook),
// which is what the "Creating your PDF" / "Exporting your spreadsheet" copy
// is promising. Lighting the card on run_report alone would show "Creating
// your PDF" for a plain question with no PDF coming — worse than no card.
//
// The image tool name was verified, not assumed: this app's own jarvis__
// tool registry (jarvis/tools/registry.py) has no "image"/"imagegen" entry
// at all — generated images come from the agent runtime's own NATIVE tool
// (unprefixed, like "bash"/"exec"/"canvas"), named "imagegen" (the fleet
// agent's own config template calls it "the agent runtime's native imagegen
// tool"; the persona's AGENTS.md/TOOLS.md call it `imagegen` too). ChatView's
// existing TOOL_PHRASES
// and this file's own WRITE_TOOLS both carry a stale "image" entry that
// backend evidence says never actually fires as a tool_name — left alone
// there (out of scope, dashboard behaviour must stay exactly as it was), but
// NOT copied into this generalized set. Only "imagegen" is wired here.

import {
	dashboardBuildPhase,
	toolBaseName,
	DASHBOARD_BUILD_PHASES,
} from "./dashboardBuildCard.js";

const PDF_TOOLS = new Set(["download_pdf", "export_document"]);
const SPREADSHEET_TOOLS = new Set(["export_excel", "export_query"]);
const IMAGE_TOOLS = new Set(["imagegen"]);

// Same "Understanding -> data -> compose -> write" shape dashboardBuildPhase
// already reads off real activeTools/statusPhase/waiting events; queried data
// tools are shared with dashboardBuildCard.js's own DATA_TOOLS (get_schema,
// get_list, run_report, query, …), and the write signal is the union of every
// artifact-producing tool above — whichever one actually ran is what decided
// `kind` in the first place, so any one of them lighting "Delivering" is
// correct for every non-dashboard kind.
const ARTIFACT_DATA_TOOLS = new Set([
	"get_schema",
	"get_list",
	"get_doc",
	"get_report_filters",
	"run_report",
	"query",
	"summarize_dataset",
	"get_creation_context",
	"resolve_links",
]);
const ARTIFACT_WRITE_TOOLS = new Set([...PDF_TOOLS, ...SPREADSHEET_TOOLS, ...IMAGE_TOOLS]);

/**
 * What artifact-producing thing (if any) the CURRENT turn is doing, from the
 * same real tool-activity signals the live card and generic activity line
 * already keep. `dashboardTurn` wins outright — it is the existing
 * builder-origin gate (isDashboardBuildTurn), evaluated before any tool has
 * necessarily run, exactly like the #858/#874 card already did.
 *
 * Tool names arrive both bare (native tools: bash/exec/canvas/imagegen) and
 * jarvis__-prefixed (registry tools: download_pdf/export_excel/…) — stripped
 * the same way dashboardBuildCard.js does before matching.
 *
 * @param {Array<{name?: string, status?: string}>} activeTools
 * @param {{dashboardTurn?: boolean}} [opts]
 * @returns {"dashboard"|"pdf"|"spreadsheet"|"image"|null}
 */
export function detectArtifactKind(activeTools, { dashboardTurn } = {}) {
	if (dashboardTurn) return "dashboard";
	const tools = Array.isArray(activeTools) ? activeTools : [];
	for (const t of tools) {
		const base = toolBaseName(t && t.name);
		if (PDF_TOOLS.has(base)) return "pdf";
		if (SPREADSHEET_TOOLS.has(base)) return "spreadsheet";
		if (IMAGE_TOOLS.has(base)) return "image";
	}
	return null;
}

/** Card title, adaptive to the detected artifact kind. */
export const ARTIFACT_TITLES = {
	dashboard: "Building your dashboard",
	pdf: "Creating your PDF",
	spreadsheet: "Exporting your spreadsheet",
	image: "Generating your image",
};

// Non-dashboard phase labels (jarvis#884). The dashboard card keeps its own
// existing DASHBOARD_BUILD_PHASES labels/wording unchanged — only these four
// generalized ones are new. Phase KEYS match dashboardBuildPhase's output
// ("understanding"/"querying"/"composing"/"publishing") so both label sets
// tick off the exact same phase function; only the display label differs.
export const ARTIFACT_BUILD_PHASES = [
	{ key: "understanding", label: "Understanding" },
	{ key: "querying", label: "Fetching data" },
	{ key: "composing", label: "Composing" },
	{ key: "publishing", label: "Delivering" },
];

/** The phase list to tick against for a given artifact kind. */
export function artifactPhaseList(kind) {
	return kind === "dashboard" ? DASHBOARD_BUILD_PHASES : ARTIFACT_BUILD_PHASES;
}

/**
 * The phase a `kind` artifact turn is honestly in right now. Dashboard turns
 * read dashboardBuildPhase exactly as before (its own tool sets, unchanged
 * behaviour); every other kind reads the same function through the
 * generalized data/write tool sets above — wrapped, not forked.
 *
 * @param {"dashboard"|"pdf"|"spreadsheet"|"image"|null} kind
 * @param {{activeTools: Array<{name?: string, status?: string}>, statusPhase: string|null, waiting: boolean}} signals
 * @returns {string|null}
 */
export function artifactBuildPhase(kind, signals) {
	if (!kind) return null;
	if (kind === "dashboard") return dashboardBuildPhase(signals);
	return dashboardBuildPhase(signals, {
		dataTools: ARTIFACT_DATA_TOOLS,
		writeTools: ARTIFACT_WRITE_TOOLS,
	});
}
