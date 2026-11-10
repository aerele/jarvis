// Real executable tests for the ```jarvis-goto contract (chatGoto.js), plus
// source assertions fencing how ChatView wires it up. Plain node built-ins
// (node:test + node:assert), the chatAsk.test.js / dashboardBuildCard.test.js
// convention: a .vue SFC cannot be imported into this runner, so its wiring is
// checked by reading the source text instead.
//
// What is being defended: main chat no longer builds a dashboard inline
// (jarvis#884) - it restates the request and points here with a fenced
// ```jarvis-goto block, ChatView stashes the prompt and redirects to the
// Dashboards builder. The parser is an ALLOWLIST (the pages in
// jarvis/public/js/shared/goto_pages.mjs: "dashboards" and "payment-calendar"),
// never generic navigation, and the redirect must fire at most once per message
// even though the block is real UI on every later visit to the transcript.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	parseGoto,
	GOTO_RE,
	gotoFiredKey,
	parseFiredStamp,
	encodeFiredStamp,
	claimGotoFire,
	builderSeed,
	requestBefore,
	gotoDecision,
	gotoCardCopy,
	gotoPageInfo,
	gotoCountdownCopy,
	hasInteractiveFence,
	focusInsideField,
	modalIsOpen,
	pruneGotoStamps,
	REQUEST_WORDS_MAX,
	REQUEST_TURNS_MAX,
} from "./chatGoto.js";
import {
	DASHBOARDS_PAGE,
	GOTO_PAGES,
	PAYMENT_CALENDAR_PAGE,
} from "../../../jarvis/public/js/shared/goto_pages.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

const fence = (body) => "Some lead-in.\n\n```jarvis-goto\n" + body + "\n```";

const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- parsing ---------------------------------------------------------------

test("parses a valid dashboards redirect", () => {
	const goto = parseGoto(fence('{"page":"dashboards","prompt":"Monthly sales by territory"}'));
	assert.deepEqual(goto, { page: "dashboards", prompt: "Monthly sales by territory" });
});

test("only the shared goto pages are allowlisted - any other page parses to null", () => {
	assert.equal(
		parseGoto(fence('{"page":"triggers","prompt":"Warn me on overdue invoices"}')),
		null
	);
	assert.equal(parseGoto(fence('{"page":"skills","prompt":"x"}')), null);
	assert.equal(parseGoto(fence('{"prompt":"no page at all"}')), null);
	assert.equal(parseGoto(fence('{"page":"","prompt":"blank page"}')), null);
	// the agent names a key, never a route or a near-miss spelling
	assert.equal(parseGoto(fence('{"page":"PaymentCalendar","prompt":"x"}')), null);
	assert.equal(parseGoto(fence('{"page":"payment_calendar","prompt":"x"}')), null);
	assert.equal(parseGoto(fence('{"page":"/dashboards/payment-calendar","prompt":"x"}')), null);
});

test("parses a valid payment-calendar redirect", () => {
	const goto = parseGoto(
		fence('{"page":"payment-calendar","prompt":" Per-day payables and cash ","extra":1}')
	);
	assert.deepEqual(goto, { page: "payment-calendar", prompt: "Per-day payables and cash" });
});

test("ALLOWED_PAGES is the shared page list, not a private copy", () => {
	const src = fs.readFileSync(path.join(HERE, "chatGoto.js"), "utf8");
	assert.match(src, /from "\.\.\/\.\.\/\.\.\/jarvis\/public\/js\/shared\/goto_pages\.mjs";/);
	assert.match(src, /const ALLOWED_PAGES = new Set\(Object\.keys\(GOTO_PAGES\)\);/);
	for (const page of Object.keys(GOTO_PAGES)) {
		assert.ok(parseGoto(fence(JSON.stringify({ page, prompt: "x" }))), `${page} must parse`);
	}
});

test("prompt must be present and non-blank, for every page", () => {
	for (const page of ["dashboards", "payment-calendar"]) {
		assert.equal(parseGoto(fence(JSON.stringify({ page }))), null, page);
		assert.equal(parseGoto(fence(JSON.stringify({ page, prompt: "" }))), null, page);
		assert.equal(parseGoto(fence(JSON.stringify({ page, prompt: "   " }))), null, page);
		assert.equal(parseGoto(fence(JSON.stringify({ page, prompt: null }))), null, page);
	}
});

test("no block, malformed JSON, and a non-object payload all parse to null", () => {
	assert.equal(parseGoto("just prose"), null);
	assert.equal(parseGoto(""), null);
	assert.equal(parseGoto(null), null);
	assert.equal(parseGoto(fence("{not json")), null);
	assert.equal(parseGoto(fence('"a string"')), null);
	assert.equal(parseGoto(fence("[1,2,3]")), null);
});

test("an unterminated block does not parse (a half-streamed reply shows no card)", () => {
	assert.equal(parseGoto('```jarvis-goto\n{"page":"dashboards","prompt":"x"'), null);
});

test("GOTO_RE matches exactly the fenced block chatBlocks.js strips", () => {
	assert.match(fence('{"page":"dashboards","prompt":"x"}'), GOTO_RE);
});

// ---- source fences: BLOCK_TAGS, the card, and the one-shot auto-redirect --

test("chatBlocks strips jarvis-goto from the visible prose", () => {
	const blocksSrc = fs.readFileSync(path.join(HERE, "chatBlocks.js"), "utf8");
	assert.match(blocksSrc, /"jarvis-goto",/);
});

test("ChatView parses goto through the shared module, not a private copy", () => {
	assert.match(
		chatViewSrc,
		/import \{\s*parseGoto,\s*gotoFiredKey,\s*claimGotoFire,\s*builderSeed,\s*requestBefore,\s*gotoDecision,\s*gotoCardCopy,\s*gotoPageInfo,\s*gotoCountdownCopy,\s*hasInteractiveFence,\s*focusInsideField,\s*modalIsOpen,\s*pruneGotoStamps,?\s*\} from "@\/lib\/chatGoto";/
	);
	const gotoOf = fnBody(chatViewSrc, "function gotoOf(m)");
	assert.match(gotoOf, /return parseGoto\(\(m && m\.content\) \|\| ""\);/);
});

// ---- the fired-stamp shape (jarvis#912) ------------------------------------

test("gotoFiredKey namespaces on the message id", () => {
	assert.equal(gotoFiredKey("msg-1"), "jarvis:goto-fired:msg-1");
});

test("a bare pre-#912 timestamp still parses, with no conversation known", () => {
	assert.deepEqual(parseFiredStamp("1723890000000"), { t: 1723890000000, conv: "" });
});

test("an unreadable/missing stamp parses to null", () => {
	assert.equal(parseFiredStamp(null), null);
	assert.equal(parseFiredStamp(""), null);
	assert.equal(parseFiredStamp("not a number"), null);
});

test("encodeFiredStamp writes the bare pre-#912 shape until a conversation is known", () => {
	assert.equal(encodeFiredStamp(123), "123");
	assert.deepEqual(parseFiredStamp(encodeFiredStamp(123)), { t: 123, conv: "" });
});

test("encodeFiredStamp/parseFiredStamp round-trip once a conversation is recorded", () => {
	const encoded = encodeFiredStamp(456, "conv-9");
	assert.deepEqual(parseFiredStamp(encoded), { t: 456, conv: "conv-9" });
});

test("the card is the GotoCard component, fed copy from the page map; the click goes through the one dispatcher", () => {
	assert.match(chatViewSrc, /import GotoCard from "@\/components\/chat\/GotoCard\.vue";/);
	assert.match(chatViewSrc, /<GotoCard\s+v-if="gotoCardOf\(m\)"/);
	assert.match(chatViewSrc, /:copy="gotoCardOf\(m\)"/);
	assert.match(chatViewSrc, /:countdown="gotoCountdownOf\(m\)"/);
	// the click always navigates through the one dispatcher
	assert.match(chatViewSrc, /@open="openGotoCard\(m\)"/);
	// the old inline card, its copy and its CSS moved into GotoCard
	assert.doesNotMatch(chatViewSrc, /jv-macrocard--goto/);
	assert.doesNotMatch(chatViewSrc, /\{\{\s*gotoCardOf\(m\)\./);
	assert.doesNotMatch(chatViewSrc, />\s*Continue in Dashboards\s*</);
	assert.doesNotMatch(chatViewSrc, />\s*Open Dashboards\s*</);
	const card = fnBody(chatViewSrc, "function gotoCardOf(m)");
	assert.match(
		card,
		/gotoCardCopy\(goto, gotoDecision\(goto, \{ canView: canViewCalendar\(\) \}\)\)/
	);
});

test("gotoCardCopy: the calendar card says what the page is, never the agent's restatement", () => {
	const calendar = { page: PAYMENT_CALENDAR_PAGE, prompt: "Per-day payables for May" };
	const copy = gotoCardCopy(calendar, gotoDecision(calendar, { canView: true }));
	assert.deepEqual(copy, {
		title: "Payment Calendar",
		button: "Open Payment Calendar",
		sub: "Day-by-day supplier dues, cash and collections, one month at a time.",
		icon: "calendar",
		note: false,
	});
	assert.ok(!JSON.stringify(copy).includes("May"), "the restatement must not reach the card");
});

test("gotoCardCopy: Dashboards keeps the restated request as its subtitle", () => {
	const dash = { page: DASHBOARDS_PAGE, prompt: "Sales by month" };
	assert.deepEqual(gotoCardCopy(dash, gotoDecision(dash, { canView: true })), {
		title: "Dashboards",
		button: "Open Dashboards",
		sub: "Sales by month",
		icon: "chart",
		note: false,
	});
	// the calendar boot flag changes nothing for a Dashboards goto
	assert.deepEqual(
		gotoCardCopy(dash, gotoDecision(dash, { canView: false })),
		gotoCardCopy(dash, gotoDecision(dash, { canView: true }))
	);
});

test("gotoCardCopy: a calendar goto the user cannot open is the Dashboards card with its reason", () => {
	const calendar = { page: PAYMENT_CALENDAR_PAGE, prompt: "Per-day payables for May" };
	for (const canView of [false, undefined]) {
		const copy = gotoCardCopy(calendar, gotoDecision(calendar, { canView }));
		assert.deepEqual(copy, {
			title: "Use Dashboards instead",
			button: "Open Dashboards",
			sub: "The Payment Calendar isn't available for your account.",
			icon: "chart",
			note: true,
		});
	}
});

test("gotoCardCopy: no goto, or no card, gives nothing; copy is translated when asked", () => {
	assert.equal(gotoCardCopy(null, gotoDecision(null, {})), null);
	assert.equal(gotoCardCopy(CALENDAR, null), null);
	globalThis.window = { __: (text) => `[xx] ${text}` };
	try {
		const copy = gotoCardCopy(CALENDAR, gotoDecision(CALENDAR, { canView: true }));
		assert.equal(
			copy.sub,
			"[xx] Day-by-day supplier dues, cash and collections, one month at a time."
		);
		const off = gotoCardCopy(CALENDAR, gotoDecision(CALENDAR, { canView: false }));
		assert.equal(off.sub, "[xx] The Payment Calendar isn't available for your account.");
		assert.equal(off.title, "[xx] Use Dashboards instead");
	} finally {
		delete globalThis.window;
	}
});

test("the card click dispatches on the decision's action and is not limited by the claim window", () => {
	const click = fnBody(chatViewSrc, "function openGotoCard(m)");
	assert.match(click, /gotoDecision\(goto, \{ canView: canViewCalendar\(\) \}\)/);
	assert.match(click, /openGotoAction\(goto, decision\.action, m\.name\);/);
	assert.doesNotMatch(click, /auto/);
	assert.doesNotMatch(click, /claimGotoFire|autoOpen/);
	const act = fnBody(
		chatViewSrc,
		'function openGotoAction(goto, action, messageId, { auto = false, runId = "" } = {})'
	);
	assert.match(act, /if \(action === PAYMENT_CALENDAR_PAGE\) \{/);
	assert.match(act, /if \(auto\) ownRuns\.delete\(runId\);/);
	// the push promise comes back, so the countdown can see a navigation failure
	assert.match(act, /return gotoPaymentCalendar\(messageId\);/);
	assert.match(act, /return gotoDashboards\(goto\.prompt, messageId\);/);
});

test("gotoPaymentCalendar: fixed route only - no prefill, no builder seed, no claim, no stamp", () => {
	const fn = fnBody(chatViewSrc, "function gotoPaymentCalendar(messageId)");
	assert.match(
		fn,
		/router\s*\.push\(\{\s*\.\.\.gotoPageInfo\(PAYMENT_CALENDAR_PAGE\)\.route,\s*state: \{ fromChat: true, chatConv: view\.conversation \},?\s*\}\)/
	);
	assert.doesNotMatch(fn, /setDashboardPrefill/);
	assert.doesNotMatch(fn, /builderSeed|requestBefore/);
	assert.doesNotMatch(fn, /claimGotoFire/);
	assert.doesNotMatch(fn, /router\.push\("/);
	// the calendar writes no stamp and never prunes any: exactly-once is the ownership token
	assert.doesNotMatch(fn, /localStorage|writeGotoStamp|pruneGotoStamps|encodeFiredStamp/);
	// the push promise is returned (the countdown settles on a failure); a caller that
	// ignores it must not leave an unhandled rejection
	assert.match(fn, /const pushed = router\s*\.push\(/);
	assert.match(fn, /pushed\.catch\(\(\) => \{\}\);\s*return pushed;/);
});

// Replays are the calendar's one-shot guard now that it keeps no stamp: the open
// consumes this tab's ownership of the run, so a duplicate run:end (a socket
// re-delivery) or a reload finds none and the decision refuses.
test("a replayed run:end does not re-open the calendar: the first open consumes the ownership token", () => {
	const dispatch = fnBody(
		chatViewSrc,
		'function openGotoAction(goto, action, messageId, { auto = false, runId = "" } = {})'
	);
	const opened = [];
	const ownRuns = new Set(["RUN-1", "RUN-2"]);
	const openGotoAction = new Function(
		"PAYMENT_CALENDAR_PAGE",
		"ownRuns",
		"gotoPaymentCalendar",
		"gotoDashboards",
		dispatch + "\n}\nreturn openGotoAction;"
	)(
		PAYMENT_CALENDAR_PAGE,
		ownRuns,
		(id) => opened.push(id),
		() => opened.push("dash")
	);
	const runEnd = (runId) => {
		// what the run:end handler does: decide from the live inputs, then open
		const d = gotoDecision(CALENDAR, { ...OPEN, ownTurn: ownRuns.has(runId) });
		if (d.autoOpen) openGotoAction(CALENDAR, d.action, "MSG-1", { auto: true, runId });
		return d.autoOpen;
	};
	assert.equal(runEnd("RUN-1"), true);
	assert.deepEqual(opened, ["MSG-1"]);
	assert.equal(runEnd("RUN-1"), false, "the replayed run:end must not open it again");
	assert.deepEqual(opened, ["MSG-1"]);
	// a card click never consumes anything, and another run is untouched
	openGotoAction(CALENDAR, PAYMENT_CALENDAR_PAGE, "MSG-1");
	assert.ok(ownRuns.has("RUN-2") && !ownRuns.has("RUN-1"));
	assert.deepEqual(opened, ["MSG-1", "MSG-1"]);
	assert.equal(runEnd("RUN-2"), true);
});

test("gotoDashboards stashes the prefill and navigates to the builder", () => {
	assert.match(
		chatViewSrc,
		/import \{ setDashboardPrefill \} from "@\/composables\/dashboardPrefill";/
	);
	const fn = fnBody(chatViewSrc, "function gotoDashboards(prompt, messageId)");
	assert.match(
		fn,
		/const text = builderSeed\(prompt, requestBefore\(messages\.value, messageId\)\);/
	);
	assert.match(fn, /setDashboardPrefill\(\{ text, autoSend: true, messageId \}\);/);
	assert.match(fn, /router\.push\("\/dashboards"\);/);
	// the stamp write (with the shared pruning helper, not a private copy of the loop)
	// goes through the guarded writer
	assert.match(fn, /if \(key\) writeGotoStamp\(key, claim\.stamp\);/);
	const writer = fnBody(chatViewSrc, "function writeGotoStamp(key, value)");
	assert.match(writer, /pruneGotoStamps\(localStorage\);/);
	assert.doesNotMatch(fn + writer, /localStorage\.key\(/);
});

// jarvis#912: a repeat hand-off for a message that already has a recorded
// builder conversation must navigate there instead of building again. See
// dashboardOpen.test.js for the DashboardsPage/DashboardChatPane side of the
// mechanism (recording the mapping, resuming it, the deleted-conversation
// fallback).
test("gotoDashboards resumes the recorded conversation for a repeat hand-off", () => {
	const fn = fnBody(chatViewSrc, "function gotoDashboards(prompt, messageId)");
	assert.match(fn, /const claim = key \? claimGotoFire\(readGotoStamp\(key\)\) /);
	assert.match(fn, /\} else if \(claim\.conv\) \{/);
	assert.match(
		fn,
		/setDashboardPrefill\(\{ text, resume: true, conv: claim\.conv, messageId \}\);/
	);
});

// jarvis#912 round 2 (finding #3): the card has no per-click guard, so two
// triggers for the same message close together (a double-click) used to both
// read "no stamp" and each start a build. claimGotoFire is the pure decision
// gotoDashboards makes that closes the race - exercised directly here since a
// .vue SFC cannot be imported into this runner (see the module header).
test("claimGotoFire: two synchronous triggers for the same message agree - only the first builds", () => {
	const now = 1_000_000;
	const first = claimGotoFire(null, now);
	assert.deepEqual(first, { build: true, stamp: String(now) });
	// what the caller (gotoDashboards) must persist before a second trigger can
	// run - the second trigger reads exactly this back
	const second = claimGotoFire(first.stamp, now);
	assert.deepEqual(second, { build: false, conv: "" });
});

test("claimGotoFire resumes a stamp that already names a conversation, regardless of age", () => {
	const encoded = encodeFiredStamp(1, "conv-9");
	assert.deepEqual(claimGotoFire(encoded, 999_999_999), { build: false, conv: "conv-9" });
});

test("claimGotoFire treats a bare stamp as expired once its claim window has passed", () => {
	// covers two cases: an abandoned claim whose send() never reached the
	// server (the tab closed before DashboardChatPane's forgetGotoClaim could
	// run), and every pre-#912 stamp already on disk - bare by construction,
	// and old enough that it must not read as "claimed" forever.
	const now = 1_000_000;
	const stale = claimGotoFire(String(now - 60_000), now);
	assert.equal(stale.build, true);
	assert.equal(stale.stamp, String(now));
});

test("claimGotoFire: no stored value at all builds", () => {
	assert.deepEqual(claimGotoFire(null, 1), { build: true, stamp: "1" });
	assert.deepEqual(claimGotoFire("", 1), { build: true, stamp: "1" });
});

// A calendar open the guards refuse (draft, fence, dialog, queued turn, expiry
// re-check) must not keep its ownership: once the user clears the composer, a
// replayed run:end would otherwise open the page late.
test("a refused calendar auto-open consumes the ownership: a replayed run:end does not navigate", () => {
	const runEnd = chatViewSrc.slice(
		chatViewSrc.indexOf('case "run:end": {'),
		chatViewSrc.indexOf('case "message:enriched": {')
	);
	const ownRuns = new Set(["RUN-1"]);
	const opened = [];
	// the refusal leg of the handler, executed: the text after the countdown start
	// is not importable (SFC), so run the one statement it contains
	const giveUp = fnBody(chatViewSrc, "function giveUpGotoAtRunEnd(p, goto)");
	assert.match(runEnd, /if \(nothingStarted\) giveUpGotoAtRunEnd\(p, goto\);/);
	const refusal = giveUp.match(
		/if \((goto && goto\.page === PAYMENT_CALENDAR_PAGE)\) (ownRuns\.delete\(p\.run_id\);)/
	);
	assert.ok(refusal, "the refused path consumes the calendar ownership");
	const consume = new Function(
		"goto",
		"PAYMENT_CALENDAR_PAGE",
		"ownRuns",
		"p",
		`if (${refusal[1]}) ${refusal[2]}`
	);
	const decide = (runId, draft) =>
		gotoDecision(CALENDAR, { ...OPEN, ownTurn: ownRuns.has(runId), draft });
	// run:end with a draft in the composer: refused, so the handler consumes it
	const first = decide("RUN-1", true);
	assert.equal(first.autoOpen, false);
	consume(CALENDAR, PAYMENT_CALENDAR_PAGE, ownRuns, { run_id: "RUN-1" });
	// the composer is cleared, then the run:end is replayed: still no navigation
	const replay = decide("RUN-1", false);
	assert.equal(replay.autoOpen, false);
	if (replay.autoOpen) opened.push("MSG-1");
	assert.deepEqual(opened, []);
	// an expiry refusal consumes it as well (settleGoto: every exit but a successful open)
	const settle = fnBody(chatViewSrc, "function settleGoto(state, outcome)");
	assert.match(settle, /ownRuns\.delete\(state\.runId\);/);
	// Dashboards logic is untouched: only a calendar goto consumes in the run:end tail
	assert.match(giveUp, /goto\.page === PAYMENT_CALENDAR_PAGE\) ownRuns\.delete/);
});

test("run:end auto-fires the redirect through gotoDecision: once, never errored or stopped", () => {
	const runEnd = chatViewSrc.slice(
		chatViewSrc.indexOf('case "run:end": {'),
		chatViewSrc.indexOf('case "message:enriched": {')
	);
	assert.match(runEnd, /const goto = m \? gotoOf\(m\) : null;/);
	assert.match(
		runEnd,
		/const decision =\s*goto && !gotoRepeat \? gotoDecision\(goto, gotoInputs\(m, p\.run_id\)\) : null;/
	);
	// the decision starts the visible countdown; nothing navigates from run:end itself
	assert.match(
		runEnd,
		/decision && decision\.autoOpen\s*\? startGotoCountdown\(m, goto, decision, p\.run_id\)\s*: null/
	);
	assert.doesNotMatch(runEnd, /openGotoAction\(/);
	// read-only: the stamp is an INPUT to the decision (a message already handled never
	// replays); the Dashboards claim itself happens in gotoDashboards (via claimGotoFire),
	// and the calendar's exactly-once is the ownership token openGotoAction consumes -
	// never here
	assert.doesNotMatch(runEnd, /localStorage\.setItem\(/);
	// the navigation is the dispatcher's, never a direct gotoDashboards() call
	assert.doesNotMatch(runEnd, /gotoDashboards\(/);
	// a refused decision drops the latch, or the morph line would animate forever
	assert.match(runEnd, /if \(nothingStarted\) giveUpGotoAtRunEnd\(p, goto\);/);
	assert.match(
		fnBody(chatViewSrc, "function giveUpGotoAtRunEnd(p, goto)"),
		/dropGotoMorph\(\);/
	);
	const inputs = fnBody(chatViewSrc, "function gotoInputs(m, runId)");
	assert.match(inputs, /error: !!m\.error,/);
	assert.match(inputs, /stopped: !!m\.stopped,/);
	assert.match(inputs, /stamp: readGotoStamp\(gotoFiredKey\(m\.name\)\),/);
	assert.match(inputs, /live: true,/);
	assert.match(inputs, /canView: canViewCalendar\(\),/);
	assert.match(inputs, /ownTurn: ownRuns\.has\(runId\),/);
	assert.match(inputs, /draft: composerHasDraft\(\),/);
	assert.match(inputs, /queued: !!queuedTurn\.value,/);
	// another interactive block in the same message, or a parked card of this turn
	assert.match(
		inputs,
		/interactive: hasInteractiveFence\(m\.content\) \|\| turnHasParkedCard\(runId\),/
	);
});

test("a parked confirmation of THIS turn, in THIS conversation, counts as interactive", () => {
	const fn = fnBody(chatViewSrc, "function turnHasParkedCard(runId)");
	const make = (cards, currentId) =>
		new Function("pendingActions", "currentId", fn + "\n}\nreturn turnHasParkedCard;")(
			{ value: cards },
			{ value: currentId }
		);
	const cards = [
		{ conversation: "C1", run_id: "R1", token: "t1" },
		{ conversation: "C2", run_id: "R2", token: "t2" },
	];
	assert.equal(make(cards, "C1")("R1"), true);
	assert.equal(make(cards, "C1")("R2"), false, "another conversation's card");
	assert.equal(make(cards, "C1")("R9"), false, "another turn's card");
	assert.equal(make(cards, "C1")(""), false, "no run id never matches");
	assert.equal(make([], "C1")("R1"), false);
});

test("the morph latch follows the decision, with the page's own morph text", () => {
	const from = chatViewSrc.indexOf("const gotoMorph = ref(null);");
	const to = chatViewSrc.indexOf("function dropGotoMorph(");
	assert.ok(from !== -1 && to > from, "latch markers must exist");
	const latch = chatViewSrc.slice(from, to);
	assert.match(
		latch,
		/gotoDecision\(g, gotoInputs\(streamingMessage\.value, currentRunId\.value\)\)\.autoOpen/
	);
	const override = fnBody(chatViewSrc, "function overrideFor(m)");
	assert.match(
		override,
		/if \(gotoMorph\.value\) return \{ text: gotoPageInfo\(gotoMorph\.value\.page\)\.morph \};/
	);
	assert.doesNotMatch(override, /Taking you to the Dashboards builder/);
});

test("a calendar auto-open is re-checked when the countdown expires (the user may have started typing)", () => {
	const expire = fnBody(chatViewSrc, "function onGotoExpire(state)");
	assert.match(
		expire,
		/found\.goto\.page === PAYMENT_CALENDAR_PAGE\s*\? gotoDecision\(found\.goto, gotoInputs\(found\.m, state\.runId\)\)\.autoOpen/
	);
});

test("the send context carries handoff_pages only under the boot flag, and no other key changes", () => {
	const send = chatViewSrc.slice(
		chatViewSrc.indexOf("let sendCtx = groundWiki"),
		chatViewSrc.indexOf("// The confirmation cards currently on screen")
	);
	assert.match(
		send,
		/if \(canViewCalendar\(\)\)\s*sendCtx = \{ \.\.\.\(sendCtx \|\| \{\}\), handoff_pages: \[PAYMENT_CALENDAR_PAGE\] \};/
	);
	// the existing keys are built exactly as before
	assert.match(
		send,
		/let sendCtx = groundWiki \? \{ ground_wiki: 1 \} : _prefillSendContext \|\| undefined;/
	);
	assert.match(send, /sendCtx = \{ \.\.\.\(sendCtx \|\| \{\}\), page: "triggers" \};/);
	assert.match(send, /focus_connector: \{ key: _sentFocus\.key, label: _sentFocus\.label \},/);
	assert.equal((send.match(/handoff_pages/g) || []).length, 1);
	const flag = fnBody(chatViewSrc, "function canViewCalendar()");
	assert.match(flag, /return window\.can_view_payment_calendar === true;/);
});

test("the goto card's narrow layout moved into GotoCard; the macro card is untouched (E8)", () => {
	// the save-as-macro card keeps the bare class and its single-row rules
	assert.equal((chatViewSrc.match(/class="jv-macrocard"/g) || []).length, 1);
	assert.doesNotMatch(chatViewSrc, /jv-macrocard--goto/);
	assert.doesNotMatch(chatViewSrc, /@media \(max-width: 400px\) \{\s*\.jv-macrocard/);
	for (const rule of [".jv-macrocard {", ".jv-macrocard-btn {", ".jv-macrocard-ic {"])
		assert.ok(chatViewSrc.includes(rule), `${rule} stays for the macro card`);
	const card = fs.readFileSync(
		path.join(HERE, "..", "components", "chat", "GotoCard.vue"),
		"utf8"
	);
	assert.match(card, /@container \(max-width: 479px\)/);
});

test("an own-tab retry and a recovered send both record their run id, capped at 50 (E3)", () => {
	const retry = fnBody(chatViewSrc, "async function retry(messageId)");
	assert.match(retry, /const r = await api\.retryMessage\(messageId\);/);
	assert.match(
		retry,
		/if \(r && r\.ok !== false\) \{\n\t+if \(r\.run_id\) rememberOwnRun\(r\.run_id\);/
	);
	const settle = fnBody(chatViewSrc, "async function settleRecoveredSend(m, r, view)");
	assert.match(settle, /if \(r\.ok !== false && r\.run_id\) rememberOwnRun\(r\.run_id\);/);
	assert.match(chatViewSrc, /const OWN_RUNS_MAX = 50;/);
	const run = new Function(
		"ownRuns",
		"OWN_RUNS_MAX",
		fnBody(chatViewSrc, "function rememberOwnRun(") + "\n}\nreturn rememberOwnRun;"
	);
	const set = new Set();
	const remember = run(set, 50);
	for (let i = 0; i < 60; i++) remember("R" + i);
	assert.equal(set.size, 50);
	assert.ok(!set.has("R9") && set.has("R10") && set.has("R59"), "oldest are dropped first");
	remember("R10"); // re-adding a known id must not evict anything
	assert.equal(set.size, 50);
	assert.ok(set.has("R10"));
});

test("ownTurn comes from runs this tab sent: filled from the accepted send, never from the page", () => {
	assert.match(chatViewSrc, /const ownRuns = new Set\(\);/);
	assert.match(
		chatViewSrc,
		/if \(r && r\.ok !== false\) \{\n\t+if \(r\.run_id\) rememberOwnRun\(r\.run_id\);/
	);
	const draft = fnBody(chatViewSrc, "function composerHasDraft()");
	assert.match(draft, /input\.value\.trim\(\)\.length > 0/);
	assert.match(draft, /pendingFiles\.value\.length > 0/);
	assert.match(draft, /micState\.value === "recording"/);
	// any OTHER field being typed in, and any open dialog (teleported: whole document)
	assert.match(
		draft,
		/focusInsideField\(document\.activeElement, rootEl\.value, composerRef\.value\?\.el\)/
	);
	assert.match(draft, /modalIsOpen\(document\)/);
});

test("composerHasDraft: executed - an open dialog or a typed-in side field is a draft, an empty focused composer is not", () => {
	const fn = fnBody(chatViewSrc, "function composerHasDraft()");
	const composerEl = el("TEXTAREA");
	const side = el("INPUT", { type: "text" });
	const build = ({ active, dialog = false, text = "" }) =>
		new Function(
			"input",
			"pendingFiles",
			"micState",
			"focusInsideField",
			"modalIsOpen",
			"document",
			"rootEl",
			"composerRef",
			fn + "\n}\nreturn composerHasDraft;"
		)(
			{ value: text },
			{ value: [] },
			{ value: "idle" },
			focusInsideField,
			modalIsOpen,
			{ activeElement: active, querySelector: () => (dialog ? {} : null) },
			{ value: rootOf(composerEl, side) },
			{ value: { el: composerEl } }
		)();
	assert.equal(build({ active: composerEl }), false, "focused but empty composer");
	assert.equal(build({ active: composerEl, text: " hi " }), true, "composer text");
	assert.equal(build({ active: side }), true, "typing in another field");
	assert.equal(build({ active: composerEl, dialog: true }), true, "a dialog is open");
});

// ---- the generic draft guard (pure helpers) --------------------------------

const el = (tagName, extra = {}) => ({ tagName, type: "", isContentEditable: false, ...extra });
const rootOf = (...inside) => ({ contains: (n) => inside.includes(n) });

test("focusInsideField: a text field, textarea or contenteditable in the chat view is a draft", () => {
	const note = el("TEXTAREA");
	const rename = el("INPUT", { type: "text" });
	const search = el("INPUT"); // no type attribute reads as text
	const rich = el("DIV", { isContentEditable: true });
	const root = rootOf(note, rename, search, rich);
	for (const field of [note, rename, search, rich]) {
		assert.equal(focusInsideField(field, root, null), true, field.tagName);
	}
});

test("focusInsideField: the main composer, buttons, checkboxes and outside fields are not a draft", () => {
	const composer = el("TEXTAREA");
	const button = el("BUTTON");
	const box = el("INPUT", { type: "checkbox" });
	const outside = el("INPUT", { type: "text" });
	const root = rootOf(composer, button, box);
	// the composer is focused after every send; its TEXT is the other draft check
	assert.equal(focusInsideField(composer, root, composer), false);
	assert.equal(focusInsideField(button, root, composer), false);
	assert.equal(focusInsideField(box, root, composer), false);
	assert.equal(focusInsideField(outside, root, composer), false, "not inside the chat view");
	assert.equal(focusInsideField(null, root, composer), false);
	assert.equal(focusInsideField(composer, null, composer), false);
});

test("modalIsOpen: any role=dialog, aria-modal or open <dialog> on the page", () => {
	const seen = [];
	const doc = (hit) => ({
		querySelector: (sel) => {
			seen.push(sel);
			return hit;
		},
	});
	assert.equal(modalIsOpen(doc({})), true);
	assert.equal(modalIsOpen(doc(null)), false);
	assert.equal(modalIsOpen(null), false);
	assert.equal(modalIsOpen({}), false);
	assert.match(seen[0], /\[role="dialog"\]/);
	assert.match(seen[0], /\[aria-modal="true"\]/);
	assert.match(seen[0], /dialog\[open\]/);
});

// ---- another interactive block in the same message -------------------------

test("hasInteractiveFence: jarvis-ask, jarvis-action and confirm blocks, closed or still streaming", () => {
	assert.equal(hasInteractiveFence('Which month?\n```jarvis-ask\n{"q":"x"}\n```'), true);
	assert.equal(hasInteractiveFence("Draft:\n```jarvis-action\n{}\n```"), true);
	assert.equal(hasInteractiveFence("Sure?\n```confirm\nSend it\n```"), true);
	assert.equal(hasInteractiveFence("Sure?\n```confirm\nSend it"), true, "unterminated");
	assert.equal(hasInteractiveFence("```jarvis-ask"), true, "opener at the very end");
});

test("hasInteractiveFence: the goto itself, other blocks and plain code are not interactive", () => {
	assert.equal(hasInteractiveFence(fence('{"page":"payment-calendar","prompt":"x"}')), false);
	assert.equal(hasInteractiveFence("```jarvis-chart\n{}\n```"), false);
	assert.equal(hasInteractiveFence("```js\nconst confirm = 1\n```"), false);
	assert.equal(hasInteractiveFence("I will confirm that for you."), false);
	assert.equal(hasInteractiveFence(""), false);
	assert.equal(hasInteractiveFence(undefined), false);
});

// ---- gotoDecision (pure) ---------------------------------------------------

// Today's dashboards decision, copied from the run:end handler as it stood before
// gotoDecision existed:  if (m && !m.error && !m.stopped) { goto = gotoOf(m);
// if (goto && !localStorage.getItem(gotoFiredKey(m.name))) { navigate } }
// The card is rendered for every parsed goto. Pinned over every input combination.
const todayDashboardsAutoOpen = ({ error, stopped, stamp }) => !error && !stopped && !stamp;
const BOOLS = [false, true];
const everyInput = function* () {
	for (const canView of BOOLS)
		for (const ownTurn of BOOLS)
			for (const draft of BOOLS)
				for (const queued of BOOLS)
					for (const live of BOOLS)
						for (const error of BOOLS)
							for (const stopped of BOOLS)
								for (const stamp of [null, "1723890000000"])
									yield {
										canView,
										ownTurn,
										draft,
										queued,
										live,
										error,
										stopped,
										stamp,
									};
};
const CALENDAR = { page: "payment-calendar", prompt: "Per-day payables" };
const DASH = { page: "dashboards", prompt: "Sales by month" };
const OPEN = {
	canView: true,
	ownTurn: true,
	draft: false,
	queued: false,
	interactive: false,
	live: true,
	error: false,
	stopped: false,
	stamp: null,
};

test("gotoDecision: Dashboards decisions are today's, whatever the calendar inputs say", () => {
	let n = 0;
	for (const input of everyInput()) {
		// the live run:end handler is the only place today's logic ran
		if (!input.live) continue;
		n++;
		assert.deepEqual(
			gotoDecision(DASH, input),
			{ card: true, autoOpen: todayDashboardsAutoOpen(input), action: "dashboards" },
			JSON.stringify(input)
		);
	}
	assert.equal(n, 128);
});

test("gotoDecision: Dashboards never auto-opens off a non-live (history) view", () => {
	for (const input of everyInput()) {
		if (input.live) continue;
		assert.equal(gotoDecision(DASH, input).autoOpen, false, JSON.stringify(input));
		assert.equal(gotoDecision(DASH, input).card, true);
	}
});

test("gotoDecision: a calendar goto opens only when every condition holds", () => {
	assert.deepEqual(gotoDecision(CALENDAR, OPEN), {
		card: true,
		autoOpen: true,
		action: "payment-calendar",
	});
	// exactly one input flipped at a time: each one alone blocks the auto-open but keeps the card
	const flips = {
		canView: false,
		ownTurn: false,
		draft: true,
		queued: true,
		interactive: true,
		live: false,
		error: true,
		stopped: true,
		stamp: "1723890000000",
	};
	for (const [key, value] of Object.entries(flips)) {
		const d = gotoDecision(CALENDAR, { ...OPEN, [key]: value });
		assert.equal(d.autoOpen, false, `${key} must block the auto-open`);
		assert.equal(d.card, true, `${key} must keep the card`);
	}
});

test("gotoDecision: calendar auto-opens iff canView, ownTurn, no draft, not queued, not interactive, live, no error, not stopped, no stamp", () => {
	let n = 0;
	for (const base of everyInput()) {
		for (const interactive of BOOLS) {
			const input = { ...base, interactive };
			n++;
			const want =
				input.canView &&
				input.ownTurn &&
				!input.draft &&
				!input.queued &&
				!input.interactive &&
				input.live &&
				!input.error &&
				!input.stopped &&
				!input.stamp;
			const d = gotoDecision(CALENDAR, input);
			assert.equal(d.autoOpen, want, JSON.stringify(input));
			assert.equal(d.card, true);
			assert.equal(d.action, input.canView ? "payment-calendar" : "dashboards");
		}
	}
	assert.equal(n, 512);
});

test("gotoDecision: another interactive block blocks the calendar auto-open but never Dashboards", () => {
	assert.deepEqual(gotoDecision(CALENDAR, { ...OPEN, interactive: true }), {
		card: true,
		autoOpen: false,
		action: "payment-calendar",
	});
	assert.equal(gotoDecision(CALENDAR, { ...OPEN, interactive: false }).autoOpen, true);
	// Dashboards is today's decision whatever this input says
	for (const interactive of BOOLS) {
		assert.deepEqual(gotoDecision(DASH, { ...OPEN, interactive }), {
			card: true,
			autoOpen: true,
			action: "dashboards",
		});
	}
});

test("gotoDecision: canView false sends a calendar goto to Dashboards, with no auto-open", () => {
	assert.deepEqual(gotoDecision(CALENDAR, { ...OPEN, canView: false }), {
		card: true,
		autoOpen: false,
		action: "dashboards",
	});
	// strict: a missing flag is false
	assert.deepEqual(gotoDecision(CALENDAR, { ...OPEN, canView: undefined }), {
		card: true,
		autoOpen: false,
		action: "dashboards",
	});
});

test("gotoDecision: no goto, or a page off the allowlist, gives no card and no action", () => {
	const none = { card: false, autoOpen: false, action: null };
	assert.deepEqual(gotoDecision(null, OPEN), none);
	assert.deepEqual(gotoDecision(undefined, OPEN), none);
	assert.deepEqual(gotoDecision({ page: "triggers", prompt: "x" }, OPEN), none);
	// a missing context is the safe default: nothing opens by itself
	assert.equal(gotoDecision(CALENDAR).autoOpen, false);
	assert.equal(gotoDecision(DASH).autoOpen, false);
});

// ---- the page map ----------------------------------------------------------

test("gotoPageInfo: labels, morph text and the fixed route for each page", () => {
	assert.deepEqual(gotoPageInfo("payment-calendar"), {
		page: "payment-calendar",
		name: "Payment Calendar",
		icon: "calendar",
		button: "Open Payment Calendar",
		morph: "Taking you to the Payment Calendar",
		route: { name: "PaymentCalendar" },
	});
	assert.deepEqual(gotoPageInfo("dashboards"), {
		page: "dashboards",
		name: "Dashboards",
		icon: "chart",
		button: "Open Dashboards",
		morph: "Taking you to the Dashboards builder",
		route: { name: "DashboardsPage" },
	});
	assert.equal(gotoPageInfo("triggers"), null);
});

test("gotoPageInfo: the route comes from the shared page list and is a fresh object each call", () => {
	for (const [page, name] of Object.entries(GOTO_PAGES)) {
		assert.deepEqual(gotoPageInfo(page).route, { name });
	}
	const a = gotoPageInfo("payment-calendar");
	a.route.name = "Elsewhere";
	assert.deepEqual(gotoPageInfo("payment-calendar").route, { name: "PaymentCalendar" });
});

test("gotoPageInfo: copy is translated when asked, not when the module loaded", () => {
	// the module is already imported above, with no translator on window
	const seen = [];
	globalThis.window = {
		__: (text) => {
			seen.push(text);
			return `[xx] ${text}`;
		},
	};
	try {
		const info = gotoPageInfo("payment-calendar");
		assert.equal(info.name, "[xx] Payment Calendar");
		assert.equal(
			"title" in info,
			false,
			"the page name is `name`; the card title comes from gotoCardCopy"
		);
		assert.equal(info.button, "[xx] Open Payment Calendar");
		assert.equal(info.morph, "[xx] Taking you to the Payment Calendar");
		assert.deepEqual(seen, [
			"Payment Calendar",
			"Open Payment Calendar",
			"Taking you to the Payment Calendar",
		]);
	} finally {
		delete globalThis.window;
	}
});

test("gotoCountdownCopy: exact strings for the visible, paused, sr-only and announced text", () => {
	const c = gotoCountdownCopy("payment-calendar", 2);
	assert.deepEqual(c, {
		visible: "Opening Payment Calendar in 2\u2026",
		srText: "Opening Payment Calendar in a few seconds. Press Escape to stay in chat.",
		announce: "Opening Payment Calendar. Press Escape to stay in chat.",
		stay: "Stay in chat",
		openNow: "Open now",
		openNowLabel: "Open now: Payment Calendar",
		opening: "Opening Payment Calendar\u2026",
		failed: "Couldn't open Payment Calendar. Try again.",
		staying: "Staying in chat.",
	});
	const paused = gotoCountdownCopy("payment-calendar", 2, true);
	assert.equal(
		paused.visible,
		"Paused \u00b7 opening Payment Calendar in 2\u2026 Move away to continue."
	);
	// the sr-only sentence says it is paused, and keeps the instruction
	assert.equal(
		paused.srText,
		"Paused. Opening Payment Calendar in a few seconds. Press Escape to stay in chat."
	);
	assert.equal(
		gotoCountdownCopy("payment-calendar", 2).srText,
		"Opening Payment Calendar in a few seconds. Press Escape to stay in chat."
	);
	assert.equal(gotoCountdownCopy("dashboards", 3).visible, "Opening Dashboards in 3\u2026");
	assert.equal(gotoCountdownCopy("dashboards", 1, true).openNowLabel, "Open now: Dashboards");
});

test("gotoCountdownCopy: the accessible name of Open now starts with its visible text (WCAG 2.5.3)", () => {
	const c = gotoCountdownCopy("dashboards", 3);
	assert.ok(c.openNowLabel.startsWith(c.openNow));
});

test("gotoCountdownCopy: an unknown page gives nothing; copy is translated when asked", () => {
	assert.equal(gotoCountdownCopy("triggers", 3), null);
	assert.equal(gotoCountdownCopy("", 3), null);
	globalThis.window = {
		__: (text, args = []) => `[xx] ${text}`.replace(/\{(\d+)\}/g, (m, i) => args[i]),
	};
	try {
		const c = gotoCountdownCopy("dashboards", 3);
		assert.equal(c.visible, "[xx] Opening [xx] Dashboards in 3\u2026");
		assert.equal(c.stay, "[xx] Stay in chat");
		assert.equal(c.opening, "[xx] Opening [xx] Dashboards\u2026");
		assert.equal(c.failed, "[xx] Couldn't open [xx] Dashboards. Try again.");
		assert.equal(c.staying, "[xx] Staying in chat.");
		assert.equal(
			gotoCountdownCopy("dashboards", 2, true).visible,
			"[xx] Paused \u00b7 opening [xx] Dashboards in 2\u2026 Move away to continue."
		);
	} finally {
		delete globalThis.window;
	}
});

// ---- resilience: storage and navigation can fail ---------------------------

test("goto stamps: a throwing localStorage never breaks the hand-off", () => {
	const reader = fnBody(chatViewSrc, "function readGotoStamp(key)");
	const writer = fnBody(chatViewSrc, "function writeGotoStamp(key, value)");
	const broken = {
		getItem: () => {
			throw new Error("SecurityError");
		},
		setItem: () => {
			throw new Error("QuotaExceededError");
		},
		length: 0,
	};
	const read = new Function("localStorage", reader + "\n}\nreturn readGotoStamp;")(broken);
	const write = new Function(
		"localStorage",
		"pruneGotoStamps",
		writer + "\n}\nreturn writeGotoStamp;"
	)(broken, pruneGotoStamps);
	assert.equal(read("k"), null, "an unreadable store reads as no stamp");
	assert.doesNotThrow(() => write("k", "1"));
	// and it still works on a healthy store
	const entries = new Map();
	const healthy = {
		getItem: (k) => (entries.has(k) ? entries.get(k) : null),
		setItem: (k, v) => entries.set(k, v),
		key: (i) => [...entries.keys()][i] ?? null,
		removeItem: (k) => entries.delete(k),
		get length() {
			return entries.size;
		},
	};
	new Function("localStorage", "pruneGotoStamps", writer + "\n}\nreturn writeGotoStamp;")(
		healthy,
		pruneGotoStamps
	)("jarvis:goto-fired:M", "5");
	assert.equal(
		new Function("localStorage", reader + "\n}\nreturn readGotoStamp;")(healthy)(
			"jarvis:goto-fired:M"
		),
		"5"
	);
	// no goto path reads or writes localStorage directly any more
	const gotoPaths = [
		"function gotoDashboards(prompt, messageId)",
		"function gotoPaymentCalendar(messageId)",
		"function gotoInputs(m, runId)",
	].map((decl) => fnBody(chatViewSrc, decl));
	for (const body of gotoPaths) assert.doesNotMatch(body, /localStorage/);
});

// ---- one constant for the page key -----------------------------------------

const withoutComments = (src) =>
	src
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.replace(/<!--[\s\S]*?-->/g, "")
		.replace(/^\s*\/\/.*$/gm, "");

test("the page key is the shared constant, not a literal, in chatGoto.js, ChatView and api.js", () => {
	const sources = {
		"chatGoto.js": fs.readFileSync(path.join(HERE, "chatGoto.js"), "utf8"),
		"ChatView.vue": chatViewSrc,
		"api.js": fs.readFileSync(path.join(HERE, "..", "api.js"), "utf8"),
	};
	for (const [name, src] of Object.entries(sources)) {
		assert.doesNotMatch(
			withoutComments(src),
			/["']payment-calendar["']/,
			`${name} must use PAYMENT_CALENDAR_PAGE`
		);
		assert.match(src, /PAYMENT_CALENDAR_PAGE/, `${name} must import the constant`);
	}
	assert.doesNotMatch(
		withoutComments(sources["chatGoto.js"]),
		/["']dashboards["']/,
		"chatGoto.js must use DASHBOARDS_PAGE"
	);
	assert.match(sources["api.js"], /from "@jsshared\/goto_pages\.mjs";/);
	assert.match(
		sources["ChatView.vue"],
		/from "\.\.\/\.\.\/\.\.\/jarvis\/public\/js\/shared\/goto_pages\.mjs";/
	);
});

// ---- the stamp pruning helper ---------------------------------------------

const fakeStorage = (entries) => {
	const map = new Map(entries);
	return {
		get length() {
			return map.size;
		},
		key: (i) => [...map.keys()][i] ?? null,
		getItem: (k) => (map.has(k) ? map.get(k) : null),
		removeItem: (k) => map.delete(k),
		has: (k) => map.has(k),
	};
};

test("pruneGotoStamps keeps the newest N goto stamps and nothing else is touched", () => {
	const entries = [["other:key", "x"]];
	for (let i = 1; i <= 60; i++) entries.push([gotoFiredKey("m" + i), String(i * 1000)]);
	entries.push([gotoFiredKey("conv"), encodeFiredStamp(5000, "c1")]);
	entries.push([gotoFiredKey("junk"), "not a stamp"]);
	const st = fakeStorage(entries);
	pruneGotoStamps(st);
	assert.ok(st.has("other:key"));
	let left = 0;
	for (let i = 0; i < st.length; i++) if (st.key(i).startsWith("jarvis:goto-fired:")) left++;
	assert.equal(left, 50);
	assert.ok(st.has(gotoFiredKey("m60")), "newest kept");
	assert.ok(!st.has(gotoFiredKey("junk")), "unreadable stamps sort oldest");
	assert.ok(!st.has(gotoFiredKey("m1")), "oldest dropped");
});

test("pruneGotoStamps does nothing at or under the limit", () => {
	const st = fakeStorage([
		[gotoFiredKey("a"), "1"],
		[gotoFiredKey("b"), "2"],
	]);
	pruneGotoStamps(st);
	assert.ok(st.has(gotoFiredKey("a")) && st.has(gotoFiredKey("b")));
	pruneGotoStamps(st, 1);
	assert.ok(!st.has(gotoFiredKey("a")) && st.has(gotoFiredKey("b")));
});

test("requestBefore does not stop at a payment-calendar goto, but still stops at a dashboards one", () => {
	const calGoto = "```jarvis-goto\n" + JSON.stringify(CALENDAR) + "\n```";
	const chat = [
		{ name: "u1", role: "user", content: "per day payables" },
		{ name: "a1", role: "assistant", content: calGoto },
		{ name: "u2", role: "user", content: "no, build me a custom dashboard" },
		{ name: "a2", role: "assistant", content: goto("Custom.") },
	];
	assert.equal(requestBefore(chat, "a2"), "per day payables\nno, build me a custom dashboard");
	const dashFirst = [
		{ name: "u1", role: "user", content: "first" },
		{ name: "a1", role: "assistant", content: goto("First.") },
		{ name: "u2", role: "user", content: "second" },
		{ name: "a2", role: "assistant", content: calGoto },
	];
	// a calendar goto is the message asked about: words after the dashboards hand-off only
	assert.equal(requestBefore(dashFirst, "a2"), "second");
});

// Aerele-RnD/jarvis-admin-v2#740 use case 3: the restatement dropped "the
// connecting lanes diagram", so the builder never drew it. The user's own words
// now ride along under the restatement.
const goto = (prompt) =>
	"```jarvis-goto\n" + JSON.stringify({ page: "dashboards", prompt }) + "\n```";
const thread = [
	{ name: "u1", role: "user", content: "hello" },
	{ name: "a1", role: "assistant", content: "Hi." },
	{
		name: "u2",
		role: "user",
		content: "  Build a dashboard. Show me the connecting lanes diagram.  ",
	},
	{ name: "t1", role: "assistant", content: "", tool_name: "jarvis__query" },
	{ name: "a2", role: "assistant", content: "Taking you to the builder." },
];

test("requestBefore: the user turns before the goto message, oldest first, trimmed", () => {
	assert.equal(
		requestBefore(thread, "a2"),
		"hello\nBuild a dashboard. Show me the connecting lanes diagram."
	);
	assert.equal(requestBefore(thread, "a1"), "hello");
});

test("requestBefore: a short answer keeps the request it answers", () => {
	const chat = [
		{ name: "u1", role: "user", content: "Coverage by sales order, with the lanes diagram." },
		{ name: "a1", role: "assistant", content: "Shall I build it as a dashboard?" },
		{ name: "u2", role: "user", content: "yes" },
		{ name: "a2", role: "assistant", content: goto("Coverage dashboard.") },
	];
	assert.equal(
		requestBefore(chat, "a2"),
		"Coverage by sales order, with the lanes diagram.\nyes"
	);
});

test("requestBefore: at most REQUEST_TURNS_MAX turns, and none from before an earlier hand-off", () => {
	const many = [1, 2, 3, 4, 5].map((n) => ({
		name: `u${n}`,
		role: "user",
		content: `turn ${n}`,
	}));
	many.push({ name: "a", role: "assistant", content: "Done." });
	assert.equal(requestBefore(many, "a"), "turn 3\nturn 4\nturn 5");
	assert.equal(REQUEST_TURNS_MAX, 3);
	const twice = [
		{ name: "u1", role: "user", content: "first dashboard" },
		{ name: "a1", role: "assistant", content: goto("First.") },
		{ name: "u2", role: "user", content: "second dashboard" },
		{ name: "a2", role: "assistant", content: goto("Second.") },
	];
	assert.equal(requestBefore(twice, "a2"), "second dashboard");
});

test("requestBefore: macro rows and the attachment marker are not the user's words", () => {
	const chat = [
		{ name: "u1", role: "user", content: "Stock by warehouse\n\n📎 stock.xlsx" },
		{ name: "u2", role: "user", content: "📎 notes.pdf" },
		{ name: "u3", role: "user", content: "▶ Running macro Weekly stock" },
		{ name: "a1", role: "assistant", content: "Taking you to the builder." },
	];
	assert.equal(requestBefore(chat, "a1"), "Stock by warehouse");
});

test("requestBefore: nothing when the message is unknown or no user turn precedes it", () => {
	assert.equal(requestBefore(thread, "missing"), "");
	assert.equal(requestBefore(thread, "u1"), "");
	assert.equal(requestBefore(null, "a2"), "");
});

test("builderSeed: the restatement first, then the user's own words", () => {
	assert.equal(
		builderSeed(
			"Coverage dashboard for pending POs and SOs.",
			"Show me the connecting lanes diagram."
		),
		"Coverage dashboard for pending POs and SOs.\n\nIn my own words: Show me the connecting lanes diagram."
	);
});

test("builderSeed: no repeat when the restatement already quotes the request, none without one", () => {
	assert.equal(
		builderSeed("Sales by month. Sales by month", "Sales by month"),
		"Sales by month. Sales by month"
	);
	assert.equal(builderSeed("  Sales by month.  ", ""), "Sales by month.");
});

test("builderSeed: a long request keeps its newest words and never splits a character", () => {
	const seed = builderSeed("Brief.", "😀" + "x".repeat(REQUEST_WORDS_MAX) + "😀");
	assert.equal(seed, `Brief.\n\nIn my own words: …${"x".repeat(REQUEST_WORDS_MAX - 1)}😀`);
});
