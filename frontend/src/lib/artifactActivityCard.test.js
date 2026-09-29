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

test("image: the verified native imagegen/image_generate tools, never the stale 'image' name", () => {
	assert.equal(detectArtifactKind([{ name: "imagegen", status: "running" }]), "image");
	// T5c: the gateway runtime's own native tool (jarvis/chat/agent_client.py
	// MEDIA_GEN_TOOL_NAMES), alongside the codex harness's "imagegen".
	assert.equal(detectArtifactKind([{ name: "image_generate", status: "running" }]), "image");
	// "image" is NOT a real tool_name (verified: absent from jarvis/tools/
	// registry.py and the agent runtime's own native tool set) — it must not
	// light the card, or a stray unrelated tool literally named "image" would.
	assert.equal(detectArtifactKind([{ name: "image", status: "running" }]), null);
	assert.equal(detectArtifactKind([{ name: "jarvis__image", status: "running" }]), null);
});

test("video_generate/music_generate are MEDIA_GEN_TOOL_NAMES siblings but never light the image card", () => {
	// "Generating your image" would be the wrong title for a video or a song —
	// only imagegen/image_generate are in IMAGE_TOOLS (T5c).
	assert.equal(detectArtifactKind([{ name: "video_generate", status: "running" }]), null);
	assert.equal(detectArtifactKind([{ name: "music_generate", status: "running" }]), null);
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

test("an image turn's write tool (imagegen or image_generate) lights the last phase", () => {
	assert.equal(
		artifactBuildPhase("image", {
			activeTools: [{ name: "imagegen", status: "running" }],
			statusPhase: null,
			waiting: false,
		}),
		"publishing"
	);
	assert.equal(
		artifactBuildPhase("image", {
			activeTools: [{ name: "image_generate", status: "running" }],
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
	// T5b: ARTIFACT_TITLES dropped from the import — the old card's own
	// {{ artifactTitle }} span is gone; liveBox (@/lib/liveTurn) builds the
	// StepsBox header's title straight from ARTIFACT_TITLES itself now.
	assert.match(
		chatSrc,
		/import \{\s*artifactBuildPhase,\s*artifactPhaseList,\s*detectArtifactKind,\s*\} from "@\/lib\/artifactActivityCard";/
	);
	assert.doesNotMatch(chatSrc, /\bARTIFACT_TITLES\b/);
	const gate = fnBody(chatSrc, "const artifactKind = computed(");
	assert.match(gate, /detectArtifactKind\(activeTools\.value,/);
	assert.match(gate, /dashboardTurn: dashboardBuildTurn\.value/);
	// the goto morph line wins outright (jarvis#884): a goto turn produces no
	// artifact, and artifactKind nulls out under the latch so the card and the
	// morph line can never both render for one turn.
	assert.match(gate, /gotoMorph\.value\s*\?\s*null\s*:/);
});

test("T5b: the standalone artifact card and the generic activity line are gone — artifactKind/artifactTickIndex now drive StepsBox's live view", () => {
	assert.doesNotMatch(chatSrc, /class="jv-artifact-live"/);
	assert.doesNotMatch(chatSrc, /class="jv-artifact-card"/);
	assert.doesNotMatch(chatSrc, /\(activeTools\.length \|\| waiting \|\| liveStep\)/);
	assert.doesNotMatch(chatSrc, /\bliveStep\b/); // the back-compat single-line computed is gone too
	// run:step still places a file turn's sentence in the artifact phase
	// that is current when it arrives (T5a), unchanged by the T5b re-mount.
	const runStepStart = chatSrc.indexOf('case "run:step": {');
	assert.notEqual(runStepStart, -1);
	const runStep = chatSrc.slice(runStepStart, chatSrc.indexOf('case "tool:start": {'));
	assert.match(runStep, /const slot = artifactKind\.value \? artifactTickIndex\.value : null;/);
	// liveBoxViewFor (the shared builder behind both the real row and the
	// synthetic in-flight one) still passes artifactKind straight to liveBox,
	// which builds the same header + phase rows the old standalone card drew.
	const liveBoxFn = fnBody(chatSrc, "function liveBoxViewFor(m) {");
	assert.match(liveBoxFn, /artifactKind: artifactKind\.value,/);
	// One box per turn (design canvas rule 2): a per-message MAP, not a
	// per-render call, mounted in Message's #above-body.
	assert.match(chatSrc, /const boxViewByMsg = computed\(\(\) =>\s*\n\tObject\.fromEntries\(/);
	assert.match(
		chatSrc,
		/<StepsBox\s*\n\s*v-if="boxViewByMsg\[m\.name\]"\s*\n\s*:view="boxViewByMsg\[m\.name\]"/
	);
});

test("the goto morph line latches on a complete streaming goto and survives run:end (does not derive from m.streaming)", () => {
	assert.match(chatSrc, /const gotoMorph = ref\(null\);/);
	const watchBody = fnBody(chatSrc, "watch(streamingGoto, (g) => {");
	assert.match(watchBody, /if \(g\) gotoMorph\.value = g;/);
	// T5b: the morph line's own markup (JarvisMark + jv-goto-morph) is gone —
	// one box per turn now — but the latch and its navigation are untouched,
	// and the exact copy is reused verbatim as boxViewFor's top-precedence
	// override (overrideFor).
	assert.doesNotMatch(chatSrc, /v-if="gotoMorph && !queuedTurn"/);
	assert.doesNotMatch(chatSrc, /class="jv-goto-morph"/);
	const overrideFn = fnBody(chatSrc, "function overrideFor(m) {");
	assert.match(
		overrideFn,
		/if \(gotoMorph\.value\) return \{ text: "Taking you to the Dashboards builder" \};/
	);
	// cleared on the next turn / an error / a stop / leaving the conversation —
	// deliberately NOT cleared in run:end, since surviving that instant (up to
	// navigation) is the entire point of the latch.
	const runStart = chatSrc.slice(
		chatSrc.indexOf('case "run:start":'),
		chatSrc.indexOf('case "queue:position":')
	);
	assert.notEqual(chatSrc.indexOf('case "run:start":'), -1);
	assert.notEqual(chatSrc.indexOf('case "queue:position":'), -1);
	// every clear site goes through dropGotoMorph(), which also cancels the
	// pending hold-navigation timer - a cleared latch with a live timer would
	// still redirect a user who moved on
	const dropFn = fnBody(chatSrc, "function dropGotoMorph() {");
	assert.match(dropFn, /clearTimeout\(gotoNavTimer\)/);
	assert.match(dropFn, /gotoMorph\.value = null;/);
	assert.match(chatSrc, /onUnmounted\(dropGotoMorph\);/);
	assert.match(runStart, /dropGotoMorph\(\);/);
	const runErrorStart = chatSrc.indexOf('case "run:error":');
	assert.notEqual(runErrorStart, -1);
	// Window widened for the review-C3 snapCandidateInto comment run:error
	// now carries ahead of its flushReveal call.
	const runError = chatSrc.slice(runErrorStart, runErrorStart + 2200);
	assert.match(runError, /dropGotoMorph\(\);/);
	const stopRun = fnBody(chatSrc, "function stopRun() {");
	assert.match(stopRun, /dropGotoMorph\(\);/);
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
	// the navigating terminal latches explicitly (the block can land WITH the
	// terminal, unseen by the streaming watcher) and holds ~1s so the morph
	// line gets real screen time - except under prefers-reduced-motion, where
	// it navigates at once (there is no animation to show)
	assert.match(runEnd, /redirected = true;/);
	assert.match(runEnd, /gotoMorph\.value = goto;/);
	assert.match(runEnd, /prefers-reduced-motion/);
	assert.match(runEnd, /GOTO_MORPH_HOLD_MS/);
	assert.match(runEnd, /if \(gotoMorph\.value\) gotoDashboards\(goto\.prompt, m\.name\);/);
	assert.match(runEnd, /if \(!redirected\) dropGotoMorph\(\);/);
	// the unconditional clear must come AFTER the redirect gate, not before it
	assert.ok(
		runEnd.indexOf("if (!redirected) dropGotoMorph();") > runEnd.indexOf("redirected = true;"),
		"the redirected check must run after the redirect branch, not race it"
	);
});
