// The "Building dashboard…" live card + finished thumbnail, for a dashboard
// build watched from MAIN chat (issue #858).
//
// Two things this file decides, both pure and both fenced by
// dashboardBuildCard.test.js so the wiring in ChatView.vue stays honest:
//
//   1. Whether the turn CURRENTLY streaming into this conversation is a
//      dashboard build, so the live progress card only ever shows for one.
//   2. What phase that build is honestly in RIGHT NOW, from the same
//      real events ChatView already keeps for its generic "Working on it…"
//      indicator (tool:start/tool:end -> activeTools, statusPhase). Never a
//      timer: a phase this mount has not seen evidence for is reported as
//      `null` (indeterminate) rather than guessed.
//
// The turn-level gate reuses the exact origin binding
// `canOpenInDashboards` (dashboardOpen.js) already uses for the finished
// artifact's "Open in Dashboards" button: `origin_page` is stamped
// server-side only when the message that opened this turn was sent with the
// Dashboards builder's `context: {page: "dashboards"}` (jarvis/chat/api.py).
// A turn earns the live card under precisely the same rule a build earns the
// promote button — main chat itself never originates one (api/dashboards.js
// is the only sender of that context), so both gates read the same fact:
// "this conversation's current activity came from the builder".

/**
 * Does the turn currently streaming into `conversation` belong to a
 * dashboard build? Same binding rule as `canOpenInDashboards`: the origin
 * must be read FOR this exact conversation, not a stale value left over from
 * the thread the user was just looking at.
 *
 * @param {{originPage: string, originOf: string, conversation: string}} arg
 */
export function isDashboardBuildTurn({ originPage, originOf, conversation }) {
	if (originPage !== "dashboards") return false;
	return !!conversation && originOf === conversation;
}

// Tool names that fetch data — the same jarvis__ registry names ChatView's
// own TOOL_PHRASES already speaks for the generic activity line (query,
// run_report, get_schema, …). Grouped here as the "Querying data" signal.
const DATA_TOOLS = new Set([
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

// Tool names that write the output artifact rather than read data. Not all
// of these are confirmed to fire for a dashboard build (only the finished
// message's `canvas` array is a verified "published" signal), so a name in
// here only lights the Publishing tick EARLY — it is never required for the
// card to reach the finished state.
const WRITE_TOOLS = new Set(["canvas", "bash", "exec", "image"]);

export const DASHBOARD_BUILD_PHASES = [
	{ key: "understanding", label: "Understanding" },
	{ key: "querying", label: "Querying data" },
	{ key: "composing", label: "Composing" },
	{ key: "publishing", label: "Publishing" },
];

function toolBaseName(name) {
	return String(name || "").replace(/^jarvis__/, "");
}

/**
 * The phase a dashboard-build turn is honestly in right now.
 *
 * Reads exactly the signals ChatView keeps for the generic activity line —
 * `activeTools` ({name, status}[] from tool:start/tool:end), `statusPhase`
 * ("analyzing" once a tool has finished and nothing else is running yet),
 * and `waiting` (true until the first event of the turn lands). A mount that
 * JOINS a turn already in flight (e.g. "Open in chat" clicked mid-stream)
 * starts with `activeTools` empty and nothing yet says which phase the turn
 * is in — that reports `null` rather than defaulting to "Understanding",
 * because this mount has not actually observed the start.
 *
 * @param {{activeTools: Array<{name?: string, status?: string}>, statusPhase: string|null, waiting: boolean}} arg
 * @returns {string|null} one of DASHBOARD_BUILD_PHASES' keys, or null
 */
export function dashboardBuildPhase({ activeTools, statusPhase, waiting }) {
	const tools = Array.isArray(activeTools) ? activeTools : [];
	const running = [...tools].reverse().find((t) => t && t.status === "running");
	if (running) {
		const base = toolBaseName(running.name);
		if (WRITE_TOOLS.has(base)) return "publishing";
		if (DATA_TOOLS.has(base)) return "querying";
		// An unrecognised tool while the turn is a confirmed dashboard build is
		// still real activity, just not one this map can name precisely —
		// "Composing" is the safest of the four to sit on, never "done".
		return "composing";
	}
	if (tools.length) {
		const sawWrite = tools.some((t) => WRITE_TOOLS.has(toolBaseName(t && t.name)));
		if (sawWrite) return "publishing";
		return "composing"; // results are in (statusPhase "analyzing") or the reply is streaming
	}
	if (waiting) return "understanding";
	return null;
}

/** Index into DASHBOARD_BUILD_PHASES for tick rendering; -1 when indeterminate. */
export function phaseTickIndex(phase) {
	return DASHBOARD_BUILD_PHASES.findIndex((p) => p.key === phase);
}

// ── the finished canvas -> a compact clickable thumbnail ────────────────────
//
// The builder's own canvas render component (DashboardCanvas.vue) is NOT
// reused here: both its modes wire the live postMessage bridge
// (callDashboardTool / runDashboardSource) so the document's queries
// actually run, and dashboardOpen.js documents that main chat deliberately
// never does that ("main chat's canvas has no query-tool bridge... that is
// accepted"). Auto-rendering that bridge for every dashboard message in a
// scrolled transcript would fire live queries per render. So the thumbnail
// reuses the SAME static srcdoc main chat already fetches for the artifact
// preview panel (`cvOf` / `api.getCanvas`), just scaled down.
//
// The canvas itself is a responsive document (no fixed design width), so
// there is no "true" size to scale from. A fixed source viewport is assumed
// — the same trick page-thumbnail UIs use — and CSS-scaled down to the
// card's own width, which is why this needs to be a pure, testable function
// rather than inline arithmetic in the template.

export const DASH_THUMB_SOURCE_WIDTH = 640;
export const DASH_THUMB_SOURCE_HEIGHT = 400;

/**
 * The iframe/container geometry for a scaled dashboard thumbnail of
 * `containerWidthPx` wide. `scale` shrinks a `DASH_THUMB_SOURCE_WIDTH`-wide
 * iframe to fit; `containerHeightPx` is what the caller sizes the
 * (overflow: hidden) wrapper to, so the scaled content never bleeds out.
 *
 * @param {number} containerWidthPx
 * @param {number} [sourceWidthPx]
 * @param {number} [sourceHeightPx]
 */
export function dashboardThumbnailTransform(
	containerWidthPx,
	sourceWidthPx = DASH_THUMB_SOURCE_WIDTH,
	sourceHeightPx = DASH_THUMB_SOURCE_HEIGHT
) {
	const w = Number(containerWidthPx) > 0 ? Number(containerWidthPx) : sourceWidthPx;
	const scale = w / sourceWidthPx;
	return {
		sourceWidthPx,
		sourceHeightPx,
		scale,
		containerHeightPx: Math.round(sourceHeightPx * scale),
	};
}
