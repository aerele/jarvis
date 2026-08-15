// "Building dashboard…" progress card + finished thumbnail (issue #858).
// Plain node built-ins (node:test + node:assert), the dashboardOpen.test.js
// convention: pure decisions live in dashboardBuildCard.js and are tested
// behaviourally here; ChatView.vue's wiring is fenced with source
// assertions because a .vue SFC cannot be imported into this runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	DASHBOARD_BUILD_PHASES,
	DASH_THUMB_SOURCE_HEIGHT,
	DASH_THUMB_SOURCE_WIDTH,
	dashboardBuildPhase,
	dashboardThumbnailTransform,
	isDashboardBuildTurn,
	isDashboardCanvas,
	phaseTickIndex,
} from "./dashboardBuildCard.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- turn gate: same origin binding as the finished artifact's promote button ----

test("a turn only earns the card when the origin was read for THIS conversation", () => {
	assert.equal(
		isDashboardBuildTurn({ originPage: "dashboards", originOf: "c1", conversation: "c1" }),
		true
	);
	// wrong page
	assert.equal(
		isDashboardBuildTurn({ originPage: "triggers", originOf: "c1", conversation: "c1" }),
		false
	);
	assert.equal(
		isDashboardBuildTurn({ originPage: "", originOf: "c1", conversation: "c1" }),
		false
	);
	// origin read for a DIFFERENT conversation than the one on screen (a stale
	// pair mid-switch) must not light the card up on the wrong thread
	assert.equal(
		isDashboardBuildTurn({ originPage: "dashboards", originOf: "c1", conversation: "c2" }),
		false
	);
	// no conversation yet (e.g. boot)
	assert.equal(
		isDashboardBuildTurn({ originPage: "dashboards", originOf: "", conversation: "" }),
		false
	);
});

// ---- phase: derived from real events only, never a timer ----

test("no activity yet, still waiting on the first event -> Understanding", () => {
	assert.equal(
		dashboardBuildPhase({ activeTools: [], statusPhase: null, waiting: true }),
		"understanding"
	);
});

test("a data tool running -> Querying data", () => {
	assert.equal(
		dashboardBuildPhase({
			activeTools: [{ name: "jarvis__query", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"querying"
	);
	assert.equal(
		dashboardBuildPhase({
			activeTools: [{ name: "get_schema", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"querying"
	);
});

test("a data tool finished, nothing else running yet -> Composing", () => {
	assert.equal(
		dashboardBuildPhase({
			activeTools: [{ name: "query", status: "completed" }],
			statusPhase: "analyzing",
			waiting: false,
		}),
		"composing"
	);
});

test("a write-ish tool running or already seen -> Publishing", () => {
	assert.equal(
		dashboardBuildPhase({
			activeTools: [{ name: "canvas", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
	assert.equal(
		dashboardBuildPhase({
			activeTools: [
				{ name: "query", status: "completed" },
				{ name: "canvas", status: "completed" },
			],
			statusPhase: "analyzing",
			waiting: false,
		}),
		"publishing"
	);
});

test("joining a turn already in flight reports null, not a guess", () => {
	// activeTools empty, not waiting either (this mount only just mounted and
	// hasn't seen the run:start / tool:start that already happened elsewhere)
	assert.equal(
		dashboardBuildPhase({ activeTools: [], statusPhase: null, waiting: false }),
		null
	);
});

test("an unmapped tool name while a build is confirmed stays Composing, not done", () => {
	assert.equal(
		dashboardBuildPhase({
			activeTools: [{ name: "run_method", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"composing"
	);
});

test("phaseTickIndex orders the four phases and reports -1 for indeterminate", () => {
	assert.equal(phaseTickIndex("understanding"), 0);
	assert.equal(phaseTickIndex("querying"), 1);
	assert.equal(phaseTickIndex("composing"), 2);
	assert.equal(phaseTickIndex("publishing"), 3);
	assert.equal(phaseTickIndex(null), -1);
	assert.equal(DASHBOARD_BUILD_PHASES.length, 4);
});

// ---- the finished canvas: dashboard identity by content, not conversation ----

test("a hosted-embed canvas item is a dashboard regardless of which conversation produced it", () => {
	assert.equal(isDashboardCanvas({ type: "html", name: "documents/abc123/index.html" }), true);
	// the dashboards skill is the only skill catalog-wide that uses the
	// hosted-embed marker (documents/<id>/index.html) — a plain canvas file
	// reference from any other skill never carries that prefix
	assert.equal(isDashboardCanvas({ type: "html", name: "charts/sales.html" }), false);
	assert.equal(isDashboardCanvas({ type: "html", name: "documents/abc/index.html" }), true);
	// wrong type: an svg/pdf/image/file under the same path is not a dashboard
	assert.equal(isDashboardCanvas({ type: "svg", name: "documents/abc/index.html" }), false);
	assert.equal(isDashboardCanvas({ type: "image", name: "documents/abc/index.html" }), false);
	// absent/malformed input never throws
	assert.equal(isDashboardCanvas(null), false);
	assert.equal(isDashboardCanvas(undefined), false);
	assert.equal(isDashboardCanvas({ type: "html" }), false);
	assert.equal(isDashboardCanvas({ type: "html", name: 42 }), false);
});

// ---- the finished canvas -> a scaled thumbnail transform ----

test("thumbnail geometry scales the fixed source viewport to the card width", () => {
	const g = dashboardThumbnailTransform(220);
	assert.equal(g.sourceWidthPx, DASH_THUMB_SOURCE_WIDTH);
	assert.equal(g.sourceHeightPx, DASH_THUMB_SOURCE_HEIGHT);
	assert.equal(g.scale, 220 / DASH_THUMB_SOURCE_WIDTH);
	assert.equal(g.containerHeightPx, Math.round(DASH_THUMB_SOURCE_HEIGHT * g.scale));
});

test("a bogus container width falls back to scale 1 rather than NaN/Infinity", () => {
	assert.equal(dashboardThumbnailTransform(0).scale, 1);
	assert.equal(dashboardThumbnailTransform(-5).scale, 1);
	assert.equal(dashboardThumbnailTransform(undefined).scale, 1);
});

test("a custom source viewport is honoured", () => {
	const g = dashboardThumbnailTransform(300, 900, 500);
	assert.equal(g.scale, 300 / 900);
	assert.equal(g.containerHeightPx, Math.round(500 * (300 / 900)));
});

// ---- ChatView wiring: source-fenced, the same precedent as dashboardOpen.test.js ----

test("ChatView imports the build-card helpers and gates the live card on the turn", () => {
	assert.match(
		chatSrc,
		/import \{\s*DASHBOARD_BUILD_PHASES,\s*dashboardBuildPhase,\s*dashboardThumbnailTransform,\s*isDashboardBuildTurn,\s*isDashboardCanvas,\s*phaseTickIndex,\s*\} from "@\/lib\/dashboardBuildCard";/
	);
	const gate = fnBody(chatSrc, "const dashboardBuildTurn = computed(");
	assert.match(gate, /originPage: originPage\.value,/);
	assert.match(gate, /originOf: originOf\.value,/);
	assert.match(gate, /conversation: currentId\.value,/);
	// the live card and the generic activity line are mutually exclusive —
	// exactly one of them can render for a given turn. The live card stays
	// origin-only: a main-chat build has no pre-canvas signal to key on (see
	// dashboardBuildCard.js's file header), so it degrades to no card rather
	// than guessing.
	assert.match(
		chatSrc,
		/v-if="dashboardBuildTurn && \(activeTools\.length \|\| waiting\) && !queuedTurn"/
	);
	assert.match(
		chatSrc,
		/v-if="\s*\(activeTools\.length \|\| waiting\) && !queuedTurn && !dashboardBuildTurn\s*"/
	);
});

test("canPromoteDashCanvas combines the builder-origin rule with canvas-presence, and the click-through gates on it too", () => {
	const combo = fnBody(chatSrc, "function canPromoteDashCanvas(cv) {");
	assert.match(combo, /canOpenDash\(cv\) \|\| isDashboardCanvas\(cv\)/);
	// openInDashboards must guard on the COMBINED gate — otherwise a click on
	// a canvas-presence-only thumbnail (no builder origin) would silently no-op
	const open = fnBody(chatSrc, "async function openInDashboards(m, cv) {");
	assert.match(open, /if \(!conv \|\| !canPromoteDashCanvas\(cv\)\) return;/);
});

test("the finished thumbnail reuses openInDashboards and the static cvOf srcdoc, never the live builder mode", () => {
	assert.match(chatSrc, /v-else-if="canPromoteDashCanvas\(cv\)"\s*\n\s*class="jv-dash-thumb"/);
	assert.match(chatSrc, /@click="openInDashboards\(m, cv\)"/);
	assert.match(chatSrc, /:srcdoc="cvOf\(m, cv\)"/);
	// DashboardCanvas.vue (the builder's live-query render component) must not
	// be pulled into ChatView — that would fire queries on every scrolled-past
	// dashboard message, which the codebase's own accepted design forbids.
	// (word-boundary: must not catch our OWN isDashboardCanvas/canPromoteDashCanvas)
	assert.doesNotMatch(chatSrc, /\bDashboardCanvas\.vue\b|<DashboardCanvas\b/);
});

test("a theme toggle re-fetches every on-screen dashboard thumbnail, not just the artifact panel", () => {
	// cvOf's cache key carries the dark flag (`${msg}::${cv}::${dark}`), and the
	// thumbnail is the first PASSIVE reader of it in the message list — nothing
	// else re-triggers ensureCanvas for it on a theme flip, so without this the
	// card would strand on its loading shimmer until an unrelated resync. Must
	// cover BOTH gates (builder-origin and canvas-presence), or a main-chat
	// build's thumbnail would strand while a builder-origin one self-heals.
	const w = chatSrc.slice(chatSrc.indexOf("watch(effectiveDark, () => {"));
	const body = w.slice(0, w.indexOf("\n});") + 4);
	assert.notEqual(chatSrc.indexOf("watch(effectiveDark, () => {"), -1);
	assert.match(body, /for \(const m of messages\.value\) \{/);
	assert.match(body, /m\.canvas\.some\(\(cv\) => canPromoteDashCanvas\(cv\)\)/);
	assert.match(body, /ensureCanvas\(m\)/);
});
