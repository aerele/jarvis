// The "Building dashboard…" live card + finished thumbnail, for a dashboard
// build watched from MAIN chat (issue #858).
//
// Corrected understanding (this file originally assumed main chat could
// never originate a build at all — wrong, see below): three things this
// file decides, all pure and all fenced by dashboardBuildCard.test.js so
// the wiring in ChatView.vue stays honest:
//
//   1. Whether the turn CURRENTLY streaming into this conversation is a
//      BUILDER-ORIGIN dashboard build, so the live progress card only ever
//      shows for one.
//   2. What phase that build is honestly in RIGHT NOW, from the same
//      real events ChatView already keeps for its generic "Working on it…"
//      indicator (tool:start/tool:end -> activeTools, statusPhase). Never a
//      timer: a phase this mount has not seen evidence for is reported as
//      `null` (indeterminate) rather than guessed.
//   3. Whether a FINISHED canvas item is a dashboard build regardless of
//      which conversation it landed in — the signal the thumbnail and its
//      click-through gate on, since origin alone misses a real path.
//
// The turn-level gate (1) reuses the exact origin binding
// `canOpenInDashboards` (dashboardOpen.js) already uses for the finished
// artifact's "Open in Dashboards" button: `origin_page` is stamped
// server-side only when the message that opened this turn was sent with the
// Dashboards builder's `context: {page: "dashboards"}` (jarvis/chat/api.py).
// That gate is real for the LIVE PROGRESS CARD specifically — it controls
// the extra system-prompt instructions turn_handler.py injects (theme
// cheatsheet, interview-first flow), which only main chat's ORDINARY
// composer never sends, so a turn watched there before the reply lands has
// no honest pre-canvas signal (see isDashboardBuildTurn's own note).
//
// It is NOT true of the finished canvas, though: `jarvis/chat/canvas.py`'s
// `persist_canvases` runs unconditionally at the end of EVERY turn, in
// every conversation, with no origin check at all (jarvis/chat/turn_handler
// .py's `_persist_canvas_and_publish`). And the jarvis-persona skill that
// produces a Dashboards build (`skills/jarvis-dashboards/SKILL.md`) is
// itself discoverable by ordinary description match in ANY chat — its own
// text: "a dashboard request in ordinary chat... still gets built here, not
// just pointed elsewhere". So a main-chat-originated dashboard canvas is a
// real, reachable case, and `isDashboardCanvas` below is the signal the
// finished-canvas UI (thumbnail, click-through) gates on instead of origin.

/**
 * Does the turn currently streaming into `conversation` belong to a
 * BUILDER-ORIGIN dashboard build? Same binding rule as `canOpenInDashboards`:
 * the origin must be read FOR this exact conversation, not a stale value
 * left over from the thread the user was just looking at.
 *
 * This is deliberately narrower than "is this turn building a dashboard" —
 * a main-chat build (no builder origin) is real but has no equivalent
 * pre-canvas signal, so a plain conversation always reports `false` here
 * and the live progress card simply does not show for it; the canvas
 * itself still gets the thumbnail treatment once it lands, via
 * `isDashboardCanvas` below.
 *
 * @param {{originPage: string, originOf: string, conversation: string}} arg
 */
export function isDashboardBuildTurn({ originPage, originOf, conversation }) {
	if (originPage !== "dashboards") return false;
	return !!conversation && originOf === conversation;
}

// The dashboards skill is the ONLY skill in the persona catalog that uses
// the agent's "hosted embed" canvas mechanism (`[embed ref="<id>" .../]`,
// jarvis/chat/canvas.py `_EMBED_REF`/`_embed_ref_name`) — every hosted-embed
// canvas item's `name` is stamped `documents/<id>/index.html` regardless of
// which conversation produced it. A plain `canvas/<path>.html` file
// reference (any other skill's chart/report/export output) never carries
// this prefix. This is the cheapest signal available without a backend
// change: it is exactly what jarvis-persona's skills/jarvis-dashboards/
// SKILL.md already promises ("that reference is what makes the canvas
// render it, whether that is the builder page's pane or a main-chat
// preview") and nothing else in the 38-skill catalog emits it.
const _HOSTED_EMBED_PREFIX = "documents/";

/**
 * Is this canvas item a dashboard build, regardless of which conversation
 * (or turn origin) produced it? The signal the finished-canvas thumbnail and
 * its "Open in Dashboards" click-through gate on — origin alone misses a
 * main-chat-originated build, which is a real, reachable case (see the file
 * header).
 *
 * @param {{type?: string, name?: string}|null|undefined} cv
 */
export function isDashboardCanvas(cv) {
	if (!cv || cv.type !== "html") return false;
	return typeof cv.name === "string" && cv.name.startsWith(_HOSTED_EMBED_PREFIX);
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
// card to reach the finished state. `save_dashboard` is the new builder-tool
// path (jarvis#884): the agent no longer builds on the agent canvas, it
// saves a Jarvis Dashboard row directly, so that tool is the write signal now.
const WRITE_TOOLS = new Set(["canvas", "bash", "exec", "image", "save_dashboard"]);

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
