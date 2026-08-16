// The common artifact-producing live card (jarvis#884), replacing the
// dashboard-only card #874 shipped. Plain node built-ins (node:test +
// node:assert), the dashboardBuildCard.test.js convention: pure decisions
// live in artifactActivityCard.js and are tested behaviourally here;
// ChatView.vue's wiring is fenced with source assertions because a .vue SFC
// cannot be imported into this runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { phaseTickIndex } from "./dashboardBuildCard.js";
import {
	ARTIFACT_BUILD_PHASES,
	ARTIFACT_TITLES,
	artifactBuildPhase,
	artifactPhaseList,
	detectArtifactKind,
} from "./artifactActivityCard.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- detectArtifactKind: dashboard wins, then whichever write tool ran ----

test("dashboardTurn wins outright, regardless of activeTools", () => {
	assert.equal(detectArtifactKind([], { dashboardTurn: true }), "dashboard");
	assert.equal(
		detectArtifactKind([{ name: "jarvis__export_excel", status: "running" }], {
			dashboardTurn: true,
		}),
		"dashboard"
	);
});

test("no dashboard turn and no recognised write tool -> null", () => {
	assert.equal(detectArtifactKind([], {}), null);
	assert.equal(detectArtifactKind([]), null);
	assert.equal(
		detectArtifactKind([{ name: "get_list", status: "completed" }], { dashboardTurn: false }),
		null
	);
});

test("run_report alone never triggers the card (a read used for ordinary Q&A)", () => {
	assert.equal(
		detectArtifactKind([{ name: "jarvis__run_report", status: "completed" }], {
			dashboardTurn: false,
		}),
		null
	);
	assert.equal(
		detectArtifactKind(
			[
				{ name: "get_schema", status: "completed" },
				{ name: "run_report", status: "completed" },
			],
			{ dashboardTurn: false }
		),
		null
	);
});

test("pdf: download_pdf or export_document, bare or jarvis__-prefixed", () => {
	assert.equal(detectArtifactKind([{ name: "download_pdf", status: "running" }]), "pdf");
	assert.equal(detectArtifactKind([{ name: "jarvis__download_pdf", status: "running" }]), "pdf");
	assert.equal(detectArtifactKind([{ name: "export_document", status: "running" }]), "pdf");
	assert.equal(
		detectArtifactKind([{ name: "jarvis__export_document", status: "completed" }]),
		"pdf"
	);
});

test("spreadsheet: export_excel or export_query, bare or jarvis__-prefixed", () => {
	assert.equal(detectArtifactKind([{ name: "export_excel", status: "running" }]), "spreadsheet");
	assert.equal(
		detectArtifactKind([{ name: "jarvis__export_excel", status: "running" }]),
		"spreadsheet"
	);
	assert.equal(detectArtifactKind([{ name: "export_query", status: "running" }]), "spreadsheet");
	assert.equal(
		detectArtifactKind([{ name: "jarvis__export_query", status: "completed" }]),
		"spreadsheet"
	);
});

test("image: the verified native imagegen tool, never the stale 'image' name", () => {
	assert.equal(detectArtifactKind([{ name: "imagegen", status: "running" }]), "image");
	// "image" is NOT a real tool_name (verified: absent from jarvis/tools/
	// registry.py and the agent runtime's own native tool set) — it must not
	// light the card, or a stray unrelated tool literally named "image" would.
	assert.equal(detectArtifactKind([{ name: "image", status: "running" }]), null);
	assert.equal(detectArtifactKind([{ name: "jarvis__image", status: "running" }]), null);
});

test("an unrecognised tool name never lights a kind", () => {
	assert.equal(detectArtifactKind([{ name: "bash", status: "running" }]), null);
	assert.equal(detectArtifactKind([{ name: "canvas", status: "running" }]), null);
	assert.equal(detectArtifactKind([{ name: "run_method", status: "running" }]), null);
});

test("the first recognised write tool in call order decides the kind", () => {
	assert.equal(
		detectArtifactKind([
			{ name: "get_schema", status: "completed" },
			{ name: "export_excel", status: "completed" },
			{ name: "download_pdf", status: "running" },
		]),
		"spreadsheet"
	);
});

// ---- adaptive copy ----------------------------------------------------------

test("every kind has a title, and only the four kinds", () => {
	assert.deepEqual(Object.keys(ARTIFACT_TITLES).sort(), [
		"dashboard",
		"image",
		"pdf",
		"spreadsheet",
	]);
	assert.equal(ARTIFACT_TITLES.dashboard, "Building your dashboard");
	assert.equal(ARTIFACT_TITLES.pdf, "Creating your PDF");
	assert.equal(ARTIFACT_TITLES.spreadsheet, "Exporting your spreadsheet");
	assert.equal(ARTIFACT_TITLES.image, "Generating your image");
});

// ---- phase mapping: generalized, but wraps dashboardBuildPhase, never forks ----

test("artifactPhaseList picks the dashboard's own labels for dashboard, the generalized four otherwise", () => {
	assert.equal(artifactPhaseList("pdf"), ARTIFACT_BUILD_PHASES);
	assert.equal(artifactPhaseList("spreadsheet"), ARTIFACT_BUILD_PHASES);
	assert.equal(artifactPhaseList("image"), ARTIFACT_BUILD_PHASES);
	assert.deepEqual(
		ARTIFACT_BUILD_PHASES.map((p) => p.label),
		["Understanding", "Fetching data", "Composing", "Delivering"]
	);
});

test("artifactBuildPhase(null, …) reports null — no kind, no phase", () => {
	assert.equal(
		artifactBuildPhase(null, { activeTools: [], statusPhase: null, waiting: true }),
		null
	);
});

test("a pdf turn: waiting -> understanding, a data tool -> fetching, the write tool -> delivering", () => {
	assert.equal(
		artifactBuildPhase("pdf", { activeTools: [], statusPhase: null, waiting: true }),
		"understanding"
	);
	assert.equal(
		artifactBuildPhase("pdf", {
			activeTools: [{ name: "jarvis__get_schema", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"querying"
	);
	assert.equal(
		artifactBuildPhase("pdf", {
			activeTools: [{ name: "jarvis__download_pdf", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
});

test("a spreadsheet turn's write tool (export_excel/export_query) lights the last phase", () => {
	assert.equal(
		artifactBuildPhase("spreadsheet", {
			activeTools: [{ name: "export_excel", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
	assert.equal(
		artifactBuildPhase("spreadsheet", {
			activeTools: [{ name: "jarvis__export_query", status: "completed" }],
			statusPhase: "analyzing",
			waiting: false,
		}),
		"publishing"
	);
});

test("an image turn's write tool (imagegen) lights the last phase", () => {
	assert.equal(
		artifactBuildPhase("image", {
			activeTools: [{ name: "imagegen", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
});

test("a dashboard turn still reads dashboardBuildPhase's own tool sets unchanged (save_dashboard, canvas)", () => {
	assert.equal(
		artifactBuildPhase("dashboard", {
			activeTools: [{ name: "jarvis__save_dashboard", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
	// export_excel is NOT one of dashboardBuildCard.js's own write tools — a
	// dashboard-origin turn reads unchanged, so this stays "Composing", never
	// "Delivering", proving the two tool sets were not merged for dashboard.
	assert.equal(
		artifactBuildPhase("dashboard", {
			activeTools: [{ name: "export_excel", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"composing"
	);
});

test("phaseTickIndex ticks the generalized phases when passed explicitly", () => {
	assert.equal(phaseTickIndex("understanding", ARTIFACT_BUILD_PHASES), 0);
	assert.equal(phaseTickIndex("querying", ARTIFACT_BUILD_PHASES), 1);
	assert.equal(phaseTickIndex("composing", ARTIFACT_BUILD_PHASES), 2);
	assert.equal(phaseTickIndex("publishing", ARTIFACT_BUILD_PHASES), 3);
	assert.equal(phaseTickIndex(null, ARTIFACT_BUILD_PHASES), -1);
});

// ---- ChatView wiring: source-fenced, the same precedent as dashboardBuildCard.test.js ----

test("ChatView imports the artifact-card helpers and detects the kind from real activity", () => {
	assert.match(
		chatSrc,
		/import \{\s*ARTIFACT_TITLES,\s*artifactBuildPhase,\s*artifactPhaseList,\s*detectArtifactKind,\s*\} from "@\/lib\/artifactActivityCard";/
	);
	const gate = fnBody(chatSrc, "const artifactKind = computed(");
	assert.match(gate, /detectArtifactKind\(activeTools\.value,/);
	assert.match(gate, /dashboardTurn: dashboardBuildTurn\.value/);
	// the goto morph line wins outright (jarvis#884): a goto turn produces no
	// artifact, and artifactKind nulls out under the latch so the card and the
	// morph line can never both render for one turn.
	assert.match(gate, /gotoMorph\.value\s*\?\s*null\s*:/);
});

test("the common card is gated on artifactKind, mutually exclusive with the morph line and the generic activity line", () => {
	assert.match(
		chatSrc,
		/v-if="artifactKind && \(activeTools\.length \|\| waiting\) && !queuedTurn"/
	);
	assert.match(
		chatSrc,
		/v-if="\s*\(activeTools\.length \|\| waiting\) &&\s*!queuedTurn &&\s*!artifactKind &&\s*!gotoMorph\s*"/
	);
});

test("the goto morph line latches on a complete streaming goto and survives run:end (does not derive from m.streaming)", () => {
	assert.match(chatSrc, /const gotoMorph = ref\(null\);/);
	const watchBody = fnBody(chatSrc, "watch(streamingGoto, (g) => {");
	assert.match(watchBody, /if \(g\) gotoMorph\.value = g;/);
	assert.match(chatSrc, /v-if="gotoMorph && !queuedTurn"/);
	// cleared on the next turn / an error / a stop / leaving the conversation —
	// deliberately NOT cleared in run:end, since surviving that instant (up to
	// navigation) is the entire point of the latch.
	const runStart = chatSrc.slice(
		chatSrc.indexOf('case "run:start":'),
		chatSrc.indexOf('case "queue:position":')
	);
	assert.notEqual(chatSrc.indexOf('case "run:start":'), -1);
	assert.notEqual(chatSrc.indexOf('case "queue:position":'), -1);
	assert.match(runStart, /gotoMorph\.value = null;/);
	const runErrorStart = chatSrc.indexOf('case "run:error":');
	assert.notEqual(runErrorStart, -1);
	const runError = chatSrc.slice(runErrorStart, runErrorStart + 2000);
	assert.match(runError, /gotoMorph\.value = null;/);
	const stopRun = fnBody(chatSrc, "function stopRun() {");
	assert.match(stopRun, /gotoMorph\.value = null;/);
	const resetRunState = fnBody(chatSrc, "function resetRunState() {");
	assert.match(resetRunState, /gotoMorph\.value = null;/);
	// run:end is the one path that can EITHER navigate (this exact terminal is
	// the one that fires gotoDashboards) or not (a second tab that lost the
	// localStorage stamp race, an errored/stopped row, or a was_recovered
	// replacement whose final text dropped the goto block). The latch must
	// only survive the navigating case — every other run:end must still drop
	// it, or a non-navigating terminal leaves the morph line animating
	// forever with no redirect coming.
	const runEnd = chatSrc.slice(
		chatSrc.indexOf('case "run:end": {'),
		chatSrc.indexOf('case "message:enriched": {')
	);
	assert.match(runEnd, /let redirected = false;/);
	assert.match(runEnd, /redirected = true;\s*\n\s*gotoDashboards\(goto\.prompt\);/);
	assert.match(runEnd, /if \(!redirected\) gotoMorph\.value = null;/);
	// the unconditional clear must come AFTER the redirect gate, not before it
	assert.ok(
		runEnd.indexOf("if (!redirected) gotoMorph.value = null;") >
			runEnd.indexOf("redirected = true;"),
		"the redirected check must run after the redirect branch, not race it"
	);
});
