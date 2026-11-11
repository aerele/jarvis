// How ChatView wires the hand-off countdown (lib/gotoCountdown.js + GotoCard.vue).
// A .vue SFC cannot be imported here, so the wiring functions are cut out of the source
// by name and EXECUTED against stubs (the chatGoto.test.js convention); what cannot be
// run (the run:end handler, the template, the CSS) is checked as source text.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gotoCountdownCopy } from "./chatGoto.js";
import { GOTO_COUNTDOWN_MS, PAUSE } from "./gotoCountdown.js";
import {
	PAYMENT_CALENDAR_PAGE,
	DASHBOARDS_PAGE,
} from "../../../jarvis/public/js/shared/goto_pages.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");
const card = fs.readFileSync(path.join(HERE, "..", "components", "chat", "GotoCard.vue"), "utf8");

const fnBody = (decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};
const runEndSrc = () => {
	const from = src.indexOf('case "run:end": {');
	const to = src.indexOf('case "message:enriched": {');
	assert.ok(from !== -1 && to > from, "run:end markers");
	return src.slice(from, to);
};

// Cut the named functions out of ChatView and bind them to `scope`.
function load(decls, names, scope) {
	const body = decls.map((d) => fnBody(d) + "\n}").join("\n");
	return new Function(...Object.keys(scope), `${body}\nreturn { ${names.join(", ")} };`)(
		...Object.values(scope)
	);
}

const DASH = { page: DASHBOARDS_PAGE, prompt: "Sales by territory" };
const CAL = { page: PAYMENT_CALENDAR_PAGE, prompt: "when are suppliers due" };

// A scope for the settle / expire / open-now functions.
function exitScope(overrides = {}) {
	const calls = {
		opened: [],
		spoken: [],
		toasts: [],
		debug: [],
		warned: [],
		removed: [],
		canView: true,
	};
	const scope = {
		calls,
		gotoCdState: { value: null },
		gotoOpening: { value: null },
		gotoMorph: { value: { page: "x", messageId: "M1" } },
		ownRuns: new Set(["RUN-1"]),
		messages: { value: [{ name: "M1", goto: DASH }] },
		gotoOf: (m) => m.goto,
		gotoInputs: () => ({}),
		// the real oracle falls back to Dashboards for a calendar goto nobody can view
		gotoDecision: (goto, opts) => ({
			autoOpen: true,
			action:
				goto.page === PAYMENT_CALENDAR_PAGE && opts && opts.canView === false
					? DASHBOARDS_PAGE
					: goto.page,
		}),
		canViewCalendar: () => calls.canView,
		composerHasDraft: () => false,
		readGotoStamp: () => null,
		gotoFiredKey: (id) => `k:${id}`,
		PAYMENT_CALENDAR_PAGE,
		// vue-router: aborted = 4, cancelled = 8, duplicated = 16; a type narrows the check
		isNavigationFailure: (r, type) => {
			const t = { FAILED: 4, CANCELLED: 8, DUPLICATED: 16 }[r];
			return t !== undefined && (type === undefined || !!(t & type));
		},
		NavigationFailureType: { aborted: 4, cancelled: 8, duplicated: 16 },
		openGotoAction: (goto, action, id, opts) => {
			calls.opened.push({ goto, action, id, opts });
			return Promise.resolve("OK");
		},
		gotoCountdownCopy,
		announceSR: (t) => calls.spoken.push(t),
		notify: (text, opts) => calls.toasts.push([text, opts]),
		onGotoStamp: () => {},
		window: { removeEventListener: (...a) => calls.removed.push(a) },
		console: {
			debug: (...a) => calls.debug.push(a),
			warn: (...a) => calls.warned.push(a),
		},
		...overrides,
	};
	return scope;
}
const EXIT_FNS = [
	"function settleGoto(state, outcome)",
	"function resolveGotoFor(state)",
	"function onGotoExpire(state)",
	"function onGotoOpenNow(state)",
	"function openGotoFromCountdown(state, goto, action)",
	"function gotoNavFailed(state, action, why)",
	"function clearGotoOpening(messageId)",
];
const EXIT_NAMES = ["settleGoto", "onGotoExpire", "onGotoOpenNow"];
const state = (over = {}) => ({
	messageId: "M1",
	runId: "RUN-1",
	page: DASHBOARDS_PAGE,
	action: DASHBOARDS_PAGE,
	secondsLeft: 1,
	paused: false,
	...over,
});
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = (scope) => load(EXIT_FNS, ["settleGoto"], scope).settleGoto;

function assertSettledEmpty(scope, why) {
	assert.equal(scope.gotoCdState.value, null, `${why}: the card state is cleared`);
	assert.equal(scope.ownRuns.has("RUN-1"), false, `${why}: ownership is consumed`);
	assert.equal(scope.gotoMorph.value, null, `${why}: the latch is dropped`);
}

// ---- one exit path ---------------------------------------------------------

test("settleGoto clears the card state; every outcome but a successful open also gives up ownership and the latch", () => {
	for (const outcome of [
		"stay",
		"esc",
		"typing",
		"send",
		"switch",
		"unmount",
		"refused",
		"navfail",
	]) {
		const scope = exitScope();
		scope.gotoCdState.value = state();
		settle(scope)(state(), outcome);
		assertSettledEmpty(scope, outcome);
	}
	const scope = exitScope();
	scope.gotoCdState.value = state();
	settle(scope)(state(), "opened");
	assert.equal(scope.gotoCdState.value, null);
	assert.equal(scope.ownRuns.has("RUN-1"), true, "a successful open keeps the run id");
	assert.ok(scope.gotoMorph.value, "and the latch, which must survive up to the navigation");
});

test("settleGoto never clears the card state of a DIFFERENT message (a late navfail)", () => {
	const scope = exitScope();
	scope.gotoCdState.value = state({ messageId: "M2", runId: "RUN-2" });
	settle(scope)(state(), "navfail");
	assert.equal(scope.gotoCdState.value.messageId, "M2");
});

test("settleGoto drops the latch only while it still belongs to that message", () => {
	const other = exitScope({ gotoMorph: { value: { page: "x", messageId: "M2" } } });
	settle(other)(state(), "stay");
	assert.equal(other.gotoMorph.value.messageId, "M2", "another message's latch survives");
	assert.equal(other.ownRuns.has("RUN-1"), false, "but this run still gives up ownership");
	const own = exitScope();
	settle(own)(state(), "stay");
	assert.equal(own.gotoMorph.value, null);
	// a successful open keeps even its own latch, as before
	const opened = exitScope();
	settle(opened)(state(), "opened");
	assert.equal(opened.gotoMorph.value.messageId, "M1");
});

test("settleGoto logs one debug line with the message, page, outcome and the reason", () => {
	for (const [reason, outcome] of [
		["opened", "opened"],
		["stay", "not-opened"],
		["esc", "not-opened"],
		["navfail", "not-opened"],
	]) {
		const scope = exitScope();
		settle(scope)(state(), reason);
		assert.deepEqual(scope.calls.debug, [
			["jarvis goto", { messageId: "M1", page: DASHBOARDS_PAGE, outcome, reason }],
		]);
	}
});

test("settleGoto says 'Staying in chat.' for Stay, Esc, typing and a refusal - and for nothing else", () => {
	for (const outcome of ["stay", "esc", "typing", "refused"]) {
		const scope = exitScope();
		settle(scope)(state(), outcome);
		assert.deepEqual(scope.calls.spoken, ["Staying in chat."], outcome);
	}
	for (const outcome of ["send", "switch", "unmount", "claimed", "navfail", "opened"]) {
		const scope = exitScope();
		settle(scope)(state(), outcome);
		assert.deepEqual(scope.calls.spoken, [], outcome);
	}
});

test("settleGoto stops watching for a second tab's claim on every exit", () => {
	for (const outcome of ["stay", "opened", "refused", "claimed", "navfail"]) {
		const scope = exitScope();
		settle(scope)(state(), outcome);
		assert.deepEqual(scope.calls.removed, [["storage", scope.onGotoStamp]], outcome);
	}
});

test("onCancel of the countdown is settleGoto with the reason", () => {
	const at = src.indexOf("const gotoCd = createGotoCountdown({");
	assert.notEqual(at, -1);
	const cfg = src.slice(at, src.indexOf("\n});", at));
	assert.match(cfg, /durationMs: GOTO_COUNTDOWN_MS,/);
	assert.match(cfg, /onTick: \(s\) => \{\s*gotoCdState\.value = s;\s*\},/);
	assert.match(cfg, /onExpire: onGotoExpire,/);
	assert.match(cfg, /onOpenNow: onGotoOpenNow,/);
	assert.match(cfg, /onCancel: \(s, reason\) => settleGoto\(s, reason\),/);
	assert.equal(GOTO_COUNTDOWN_MS, 3000);
});

// ---- timer expiry ----------------------------------------------------------

test("expiry opens through the dispatcher as the auto-open, naming the run, then settles as opened", async () => {
	const scope = exitScope();
	scope.gotoCdState.value = state();
	load(EXIT_FNS, EXIT_NAMES, scope).onGotoExpire(state());
	assert.equal(scope.calls.opened.length, 1);
	assert.deepEqual(scope.calls.opened[0].opts, { auto: true, runId: "RUN-1" });
	assert.equal(scope.calls.opened[0].action, DASHBOARDS_PAGE);
	assert.equal(scope.gotoCdState.value, null);
	assert.equal(scope.ownRuns.has("RUN-1"), true);
	await tick();
	assert.equal(scope.ownRuns.has("RUN-1"), true, "a clean navigation changes nothing more");
});

const REFUSALS = [
	["the message is gone", { messages: { value: [] } }, {}],
	["its goto changed page", { messages: { value: [{ name: "M1", goto: CAL }] } }, {}],
	["its goto is gone", { messages: { value: [{ name: "M1", goto: null }] } }, {}],
	["the composer has a draft (Dashboards)", { composerHasDraft: () => true }, {}],
	[
		"the composer has a draft (calendar)",
		{
			composerHasDraft: () => true,
			messages: { value: [{ name: "M1", goto: CAL }] },
		},
		{ page: PAYMENT_CALENDAR_PAGE, action: PAYMENT_CALENDAR_PAGE },
	],
	["another tab claimed the Dashboards stamp", { readGotoStamp: () => "123" }, {}],
	[
		"the calendar decision no longer allows it",
		{
			gotoDecision: () => ({ autoOpen: false }),
			messages: { value: [{ name: "M1", goto: CAL }] },
		},
		{ page: PAYMENT_CALENDAR_PAGE, action: PAYMENT_CALENDAR_PAGE },
	],
];
for (const [why, over, stateOver] of REFUSALS)
	test(`expiry refuses and settles when ${why}`, () => {
		const scope = exitScope(over);
		scope.gotoCdState.value = state(stateOver);
		load(EXIT_FNS, EXIT_NAMES, scope).onGotoExpire(state(stateOver));
		assert.equal(scope.calls.opened.length, 0, "nothing opens");
		assertSettledEmpty(scope, why);
		assert.deepEqual(scope.calls.spoken, ["Staying in chat."], "the refusal is announced");
	});

test("expiry that throws still settles: the row cannot stick and Esc is not swallowed", () => {
	const calendar = { page: PAYMENT_CALENDAR_PAGE, action: PAYMENT_CALENDAR_PAGE };
	for (const [why, over, stateOver] of [
		[
			"the decision throws",
			{
				messages: { value: [{ name: "M1", goto: CAL }] },
				gotoDecision: () => {
					throw new Error("boom");
				},
			},
			calendar,
		],
		[
			"the draft check throws",
			{
				composerHasDraft: () => {
					throw new Error("boom");
				},
			},
			{},
		],
		[
			"the dispatcher throws",
			{
				openGotoAction: () => {
					throw new Error("boom");
				},
			},
			{},
		],
	]) {
		const scope = exitScope(over);
		scope.gotoCdState.value = state(stateOver);
		assert.throws(
			() => load(EXIT_FNS, EXIT_NAMES, scope).onGotoExpire(state(stateOver)),
			/boom/,
			why
		);
		assertSettledEmpty(scope, why);
	}
});

test("expiry has no shortcut around settleGoto: one try/finally owns the exit", () => {
	const body = fnBody("function onGotoExpire(state)");
	assert.match(body, /try \{/);
	assert.match(body, /\} finally \{\s*settleGoto\(state, outcome\);\s*\}/);
});

test("the Dashboards stamp is read for THIS message at expiry; the calendar ignores it", () => {
	const keys = [];
	const scope = exitScope({
		readGotoStamp: (k) => (keys.push(k), null),
	});
	load(EXIT_FNS, EXIT_NAMES, scope).onGotoExpire(state());
	assert.deepEqual(keys, ["k:M1"]);
	const cal = exitScope({
		readGotoStamp: (k) => (keys.push(k), "123"),
		messages: { value: [{ name: "M1", goto: CAL }] },
	});
	keys.length = 0;
	load(EXIT_FNS, EXIT_NAMES, cal).onGotoExpire(
		state({ page: PAYMENT_CALENDAR_PAGE, action: PAYMENT_CALENDAR_PAGE })
	);
	assert.deepEqual(keys, [], "the calendar keeps no stamp");
	assert.equal(cal.calls.opened.length, 1);
});

test("expiry source: the message, the draft, the stamp and the navigation failure are all checked", () => {
	const expire = fnBody("function onGotoExpire(state)");
	assert.match(expire, /resolveGotoFor\(state\)/);
	assert.match(expire, /composerHasDraft\(\)/);
	assert.match(expire, /readGotoStamp\(gotoFiredKey\(state\.messageId\)\)/);
	assert.match(expire, /let outcome = "refused"/);
	const resolve = fnBody("function resolveGotoFor(state)");
	assert.match(resolve, /messages\.value\.find\(\(x\) => x\.name === state\.messageId\)/);
	const open = fnBody("function openGotoFromCountdown(state, goto, action)");
	assert.match(open, /isNavigationFailure\(/);
	assert.match(open, /gotoNavFailed\(/);
	assert.match(open, /return "opened";/);
	assert.match(open, /return "refused";/);
});

test("expiry and Open now share one resolver for the message's current goto", () => {
	assert.match(fnBody("function onGotoExpire(state)"), /resolveGotoFor\(state\)/);
	assert.match(fnBody("function onGotoOpenNow(state)"), /resolveGotoFor\(state\)/);
	const scope = exitScope({
		messages: {
			value: [
				{ name: "M1", goto: CAL },
				{ name: "M2", goto: null },
			],
		},
	});
	const { resolveGotoFor } = load(EXIT_FNS, ["resolveGotoFor"], scope);
	assert.equal(resolveGotoFor(state()).goto, CAL);
	assert.equal(resolveGotoFor(state()).m.name, "M1");
	assert.equal(resolveGotoFor(state({ messageId: "M2" })), null, "its goto is gone");
	assert.equal(resolveGotoFor(state({ messageId: "M9" })), null, "the message is gone");
});

// ---- Open now --------------------------------------------------------------

test("Open now is never refused: no draft, stamp or autoOpen check, only a vanished goto", () => {
	const scope = exitScope({
		composerHasDraft: () => true,
		readGotoStamp: () => "123",
		gotoDecision: (goto) => ({ autoOpen: false, action: goto.page }),
	});
	scope.gotoCdState.value = state();
	load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state());
	assert.equal(scope.calls.opened.length, 1);
	assert.deepEqual(scope.calls.opened[0].opts, { auto: true, runId: "RUN-1" });
	assert.equal(scope.gotoCdState.value, null);
	const body = fnBody("function onGotoOpenNow(state)");
	// the decision is read only for the ACTION (what the user can open now), never its autoOpen
	assert.doesNotMatch(body, /composerHasDraft|readGotoStamp|gotoInputs|autoOpen/);

	const gone = exitScope({ messages: { value: [{ name: "M1", goto: null }] } });
	gone.gotoCdState.value = state();
	load(EXIT_FNS, EXIT_NAMES, gone).onGotoOpenNow(state());
	assert.equal(gone.calls.opened.length, 0, "the goto is gone: nothing to open");
	assertSettledEmpty(gone, "gone");
});

test("Open now opens the CURRENT goto: its own page and the action for what the user can open now", () => {
	// the message's goto changed from Dashboards to the calendar while the countdown ran
	const changed = exitScope({ messages: { value: [{ name: "M1", goto: CAL }] } });
	changed.gotoCdState.value = state(); // the state still says Dashboards
	load(EXIT_FNS, EXIT_NAMES, changed).onGotoOpenNow(state());
	assert.equal(changed.calls.opened.length, 1);
	assert.equal(changed.calls.opened[0].goto, CAL);
	assert.equal(
		changed.calls.opened[0].action,
		PAYMENT_CALENDAR_PAGE,
		"not the state's stale action"
	);
});

test("Open now on a calendar countdown whose access was lost does not hand off: settles refused", () => {
	const lost = exitScope({ messages: { value: [{ name: "M1", goto: CAL }] } });
	lost.calls.canView = false;
	const cdState = state({ page: PAYMENT_CALENDAR_PAGE, action: PAYMENT_CALENDAR_PAGE });
	lost.gotoCdState.value = cdState;
	load(EXIT_FNS, EXIT_NAMES, lost).onGotoOpenNow(cdState);
	assert.equal(lost.calls.opened.length, 0, "no navigation, to Dashboards or anywhere");
	assert.equal(lost.gotoOpening.value, null);
	assert.equal(lost.calls.debug[0][1].reason, "refused");
	assertSettledEmpty(lost, "lost access");
});

test("Open now that throws still settles", () => {
	const scope = exitScope({
		openGotoAction: () => {
			throw new Error("boom");
		},
	});
	scope.gotoCdState.value = state();
	assert.throws(() => load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state()), /boom/);
	assertSettledEmpty(scope, "throw");
});

// ---- "Opening…" between the open and the arrival ----------------------------

test("a started navigation keeps the card in its Opening state until the push settles", async () => {
	let finish;
	const scope = exitScope({
		openGotoAction: () => new Promise((resolve) => (finish = resolve)),
	});
	scope.gotoCdState.value = state();
	load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state());
	assert.equal(scope.gotoCdState.value, null, "the countdown row is gone");
	assert.deepEqual(scope.gotoOpening.value, { messageId: "M1", page: DASHBOARDS_PAGE });
	await tick();
	assert.ok(scope.gotoOpening.value, "still opening while the push is pending");
	finish("OK");
	await tick();
	assert.equal(scope.gotoOpening.value, null, "cleared when the push settles");
});

test("a refused or failed open leaves no Opening state; another message's survives", async () => {
	const refused = exitScope({ openGotoAction: () => undefined });
	load(EXIT_FNS, EXIT_NAMES, refused).onGotoOpenNow(state());
	assert.equal(refused.gotoOpening.value, null);

	const failed = exitScope({ openGotoAction: () => Promise.resolve("FAILED") });
	load(EXIT_FNS, EXIT_NAMES, failed).onGotoOpenNow(state());
	assert.ok(failed.gotoOpening.value);
	await tick();
	assert.equal(failed.gotoOpening.value, null);

	const other = exitScope({ openGotoAction: () => Promise.resolve("OK") });
	other.gotoOpening.value = { messageId: "M2", page: DASHBOARDS_PAGE };
	const { clearGotoOpening } = load(EXIT_FNS, ["clearGotoOpening"], other);
	clearGotoOpening("M1");
	assert.equal(other.gotoOpening.value.messageId, "M2");
});

// ---- navigation failure ----------------------------------------------------

test("a navigation failure result or a rejection after the push settles as navfail", async () => {
	for (const push of [
		() => Promise.resolve("FAILED"),
		() => Promise.reject(new Error("chunk")),
	]) {
		const scope = exitScope({
			openGotoAction: () => push(),
		});
		scope.gotoCdState.value = state();
		load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state());
		assert.equal(scope.ownRuns.has("RUN-1"), true, "still own right after the push");
		await tick();
		assertSettledEmpty(scope, "navfail");
		assert.equal(scope.gotoOpening.value, null);
	}
});

test("a navigation failure tells the user: a toast and an announcement with the same words, plus a console warning", async () => {
	for (const [push, why] of [
		[() => Promise.resolve("FAILED"), "FAILED"],
		[() => Promise.reject(new Error("chunk")), undefined],
	]) {
		const scope = exitScope({ openGotoAction: push });
		scope.gotoCdState.value = state();
		load(EXIT_FNS, EXIT_NAMES, scope).onGotoExpire(state());
		await tick();
		const text = "Couldn't open Dashboards. Try again.";
		assert.deepEqual(scope.calls.toasts, [[text, { type: "error" }]]);
		assert.deepEqual(scope.calls.spoken, [text]);
		assert.equal(scope.calls.warned.length, 1);
		const [msg, page, detail] = scope.calls.warned[0];
		assert.equal(msg, "Jarvis goto: navigation failed");
		assert.equal(page, DASHBOARDS_PAGE);
		if (why) assert.equal(detail, why);
		else assert.match(detail.message, /chunk/);
	}
});

test("a cancelled or duplicated navigation is not a failure: no toast, no warning", async () => {
	for (const result of ["CANCELLED", "DUPLICATED", "OK", undefined]) {
		const scope = exitScope({ openGotoAction: () => Promise.resolve(result) });
		scope.gotoCdState.value = state();
		load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state());
		await tick();
		assert.deepEqual(scope.calls.toasts, [], String(result));
		assert.deepEqual(scope.calls.warned, [], String(result));
		assert.equal(scope.ownRuns.has("RUN-1"), true, "ownership is kept");
	}
	const open = fnBody("function openGotoFromCountdown(state, goto, action)");
	assert.match(open, /NavigationFailureType\.cancelled \| NavigationFailureType\.duplicated/);
});

test("when the dispatcher returns nothing (the view moved on) the exit settles as refused", () => {
	const scope = exitScope({ openGotoAction: () => undefined });
	scope.gotoCdState.value = state();
	load(EXIT_FNS, EXIT_NAMES, scope).onGotoOpenNow(state());
	assertSettledEmpty(scope, "no push");
});

// ---- starting --------------------------------------------------------------

function startScope(overrides = {}) {
	const log = { started: [], handled: [], spoken: [], scrolled: [], listeners: [] };
	const scope = {
		log,
		document: { visibilityState: "visible" },
		window: {
			matchMedia: () => ({ matches: false }),
			addEventListener: (...a) => log.listeners.push(a),
		},
		composerHasDraft: () => false,
		messages: { value: [{ name: "M0" }, { name: "M1" }] },
		pinnedToBottom: { value: true },
		onGotoStamp: () => {},
		PAYMENT_CALENDAR_PAGE,
		gotoCd: {
			markHandled: (id) => log.handled.push(id),
			start: (a) => (log.started.push(a), true),
			state: () => ({ secondsLeft: 3 }),
		},
		gotoMorph: { value: null },
		gotoCountdownCopy,
		announceSR: (t) => log.spoken.push(t),
		nextTick: (f) => f(),
		gotoCardRefs: new Map([["M1", { $el: { scrollIntoView: (o) => log.scrolled.push(o) } }]]),
		...overrides,
	};
	return scope;
}
const START_FNS = [
	"function startGotoCountdown(m, goto, decision, runId)",
	"function scrollGotoCardIntoView(id)",
];
const startIt = (scope, m, goto, decision, runId = "RUN-1") =>
	load(START_FNS, ["startGotoCountdown"], scope).startGotoCountdown(m, goto, decision, runId);

test("a visible tab starts the countdown, latches the morph, announces once and scrolls the card in", () => {
	const scope = startScope();
	const { startGotoCountdown } = load(START_FNS, ["startGotoCountdown"], scope);
	const copy = startGotoCountdown(
		{ name: "M1" },
		CAL,
		{ action: PAYMENT_CALENDAR_PAGE },
		"RUN-1"
	);
	assert.deepEqual(scope.log.started, [
		{
			messageId: "M1",
			runId: "RUN-1",
			page: PAYMENT_CALENDAR_PAGE,
			action: PAYMENT_CALENDAR_PAGE,
		},
	]);
	assert.deepEqual(scope.gotoMorph.value, { ...CAL, messageId: "M1" }, "the latch is tagged");
	assert.deepEqual(scope.log.spoken, [
		"Opening Payment Calendar. Press Escape to stay in chat.",
	]);
	assert.equal(copy.announce, scope.log.spoken[0]);
	assert.deepEqual(scope.log.scrolled, [{ block: "nearest", behavior: "smooth" }]);
	assert.deepEqual(scope.log.handled, []);
});

test("reduced motion scrolls the card in without animation ('auto', not 'instant') but keeps the countdown", () => {
	const scope = startScope({
		window: { matchMedia: () => ({ matches: true }), addEventListener: () => {} },
	});
	startIt(scope, { name: "M1" }, DASH, { action: DASHBOARDS_PAGE });
	assert.equal(scope.log.started.length, 1);
	assert.deepEqual(scope.log.scrolled, [{ block: "nearest", behavior: "auto" }]);
});

test("the card is scrolled in only when it is in the LAST message and the list is pinned to the bottom", () => {
	const cases = [
		["last and pinned", { messages: { value: [{ name: "M0" }, { name: "M1" }] } }, 1],
		["not the last message", { messages: { value: [{ name: "M1" }, { name: "M2" }] } }, 0],
		["the user scrolled up", { pinnedToBottom: { value: false } }, 0],
		["no messages", { messages: { value: [] } }, 0],
	];
	for (const [why, over, want] of cases) {
		const scope = startScope(over);
		startIt(scope, { name: "M1" }, DASH, { action: DASHBOARDS_PAGE });
		assert.equal(scope.log.started.length, 1, `${why}: the countdown still starts`);
		assert.equal(scope.log.scrolled.length, want, why);
	}
});

// M1: a draft already in the composer at run:end means no countdown at all - it would only
// have run to a silent refusal.
test("a draft in the composer at run:end starts no countdown on either page: card only, handled, no announcement", () => {
	for (const [goto, action] of [
		[DASH, DASHBOARDS_PAGE],
		[CAL, PAYMENT_CALENDAR_PAGE],
	]) {
		const scope = startScope({ composerHasDraft: () => true });
		const r = startIt(scope, { name: "M1" }, goto, { action });
		assert.equal(r, null, goto.page);
		assert.deepEqual(scope.log.started, [], goto.page);
		assert.deepEqual(scope.log.handled, ["M1"], "marked handled: no later run:end starts one");
		assert.deepEqual(scope.log.spoken, [], "the normal 'replied.' line stays the only one");
		assert.equal(scope.gotoMorph.value, null);
		assert.deepEqual(scope.log.listeners, []);
	}
});

test("startGotoCountdown checks the draft in the same guard as the hidden tab, before it starts anything", () => {
	const body = fnBody("function startGotoCountdown(m, goto, decision, runId)");
	assert.match(
		body,
		/if \(document\.visibilityState !== "visible" \|\| composerHasDraft\(\)\) \{\s*gotoCd\.markHandled\(m\.name\);\s*return null;\s*\}/
	);
	assert.ok(body.indexOf("composerHasDraft()") < body.indexOf("gotoCd.start("));
});

test("a Dashboards countdown watches for a second tab's claim; the calendar one does not", () => {
	const dash = startScope();
	startIt(dash, { name: "M1" }, DASH, { action: DASHBOARDS_PAGE });
	assert.deepEqual(dash.log.listeners, [["storage", dash.onGotoStamp]]);
	const cal = startScope();
	startIt(cal, { name: "M1" }, CAL, { action: PAYMENT_CALENDAR_PAGE });
	assert.deepEqual(cal.log.listeners, []);
	const refused = startScope();
	refused.gotoCd.start = () => false;
	startIt(refused, { name: "M1" }, DASH, { action: DASHBOARDS_PAGE });
	assert.deepEqual(refused.log.listeners, [], "a refused start watches nothing");
});

test("a storage event for THIS message's fired stamp cancels the running Dashboards countdown as 'claimed'", () => {
	const cancels = [];
	let running = { messageId: "M1" };
	const scope = {
		gotoCd: { state: () => running, cancel: (r) => cancels.push(r) },
		gotoFiredKey: (id) => `k:${id}`,
	};
	const { onGotoStamp } = load(["function onGotoStamp(e)"], ["onGotoStamp"], scope);
	onGotoStamp({ key: "k:M2", newValue: "123" }); // another message
	onGotoStamp({ key: "k:M1", newValue: null }); // a removal (pruning), not a claim
	onGotoStamp({ key: null, newValue: null }); // storage.clear()
	assert.deepEqual(cancels, []);
	onGotoStamp({ key: "k:M1", newValue: "123" });
	assert.deepEqual(cancels, ["claimed"]);
	running = null;
	onGotoStamp({ key: "k:M1", newValue: "123" });
	assert.deepEqual(cancels, ["claimed"], "no countdown, nothing to cancel");
});

test("a hidden tab marks the message handled, starts nothing, announces nothing", () => {
	for (const state of ["hidden", "prerender"]) {
		const scope = startScope({ document: { visibilityState: state } });
		const { startGotoCountdown } = load(START_FNS, ["startGotoCountdown"], scope);
		const r = startGotoCountdown({ name: "M1" }, DASH, { action: DASHBOARDS_PAGE }, "RUN-1");
		assert.equal(r, null);
		assert.deepEqual(scope.log.handled, ["M1"]);
		assert.deepEqual(scope.log.started, []);
		assert.deepEqual(scope.log.spoken, []);
		assert.equal(scope.gotoMorph.value, null);
	}
});

test("a refused start (already handled) returns null and announces nothing", () => {
	const scope = startScope();
	scope.gotoCd.start = () => false;
	const { startGotoCountdown } = load(START_FNS, ["startGotoCountdown"], scope);
	assert.equal(startGotoCountdown({ name: "M1" }, DASH, { action: DASHBOARDS_PAGE }, "R"), null);
	assert.deepEqual(scope.log.spoken, []);
	assert.equal(scope.gotoMorph.value, null);
});

// ---- run:end (source) ------------------------------------------------------

test("run:end: a countdown replaces the 'replied.' announcement, and only that case", () => {
	const runEnd = runEndSrc();
	assert.match(
		runEnd,
		/const replayDuringCountdown = gotoRepeat && gotoCd\.state\(\)\?\.messageId === m\.name;/
	);
	assert.match(
		runEnd,
		/if \(!countdown && !replayDuringCountdown\)\s+announceSR\(`\$\{brand\.agentName\} replied\.`\);/
	);
	assert.equal((runEnd.match(/announceSR\(`\$\{brand\.agentName\} replied\./g) || []).length, 1);
	// the one announcement of the countdown is the helper's, not run:end's
	assert.doesNotMatch(runEnd, /\.announce\b/);
	const start = fnBody("function startGotoCountdown(m, goto, decision, runId)");
	assert.equal((start.match(/announceSR\(/g) || []).length, 1);
});

test("run:end: a duplicate or already-handled run:end does nothing in the goto branch, tail skipped", () => {
	const runEnd = runEndSrc();
	assert.match(runEnd, /const gotoRepeat = !!goto && gotoCd\.isHandled\(m\.name\);/);
	// no decision, no start, for a repeat
	assert.match(runEnd, /goto && !gotoRepeat \? gotoDecision\(/);
	// the tail (ownership give-up + latch drop) is skipped for a repeat
	assert.match(runEnd, /const nothingStarted = !countdown && !gotoRepeat;/);
	const tail = runEnd.slice(runEnd.indexOf("if (nothingStarted)"));
	assert.match(tail, /^if \(nothingStarted\) giveUpGotoAtRunEnd\(p, goto\);/);
	// nothing in the goto branch touches ownRuns/dropGotoMorph outside that guard
	const before = runEnd.slice(0, runEnd.indexOf("if (nothingStarted)"));
	assert.doesNotMatch(before, /ownRuns\.delete|dropGotoMorph\(|gotoMorph\.value = null/);
});

// ---- run:end with no countdown of its own ------------------------------------

// `running` is what gotoCd.state() returns; `latch` the gotoMorph value.
function giveUp(running, latch, p, goto) {
	const log = { dropped: 0 };
	const scope = {
		log,
		ownRuns: new Set(["RUN-1", "RUN-2"]),
		gotoCd: { state: () => running },
		gotoMorph: { value: latch },
		PAYMENT_CALENDAR_PAGE,
		dropGotoMorph: () => {
			log.dropped++;
			scope.gotoMorph.value = null;
		},
	};
	load(
		["function giveUpGotoAtRunEnd(p, goto)"],
		["giveUpGotoAtRunEnd"],
		scope
	).giveUpGotoAtRunEnd(p, goto);
	return scope;
}

test("run:end without a countdown gives up a calendar run's ownership and drops the latch and any countdown of its own", () => {
	const own = giveUp(null, { messageId: "M1" }, { run_id: "RUN-1", message_id: "M1" }, CAL);
	assert.equal(own.ownRuns.has("RUN-1"), false);
	assert.equal(own.ownRuns.has("RUN-2"), true);
	assert.equal(own.log.dropped, 1);
	// a Dashboards goto has no ownership token to give up
	const dash = giveUp(null, null, { run_id: "RUN-1", message_id: "M1" }, DASH);
	assert.equal(dash.ownRuns.has("RUN-1"), true);
	assert.equal(dash.log.dropped, 1);
	// no goto at all (a plain reply): the latch still goes
	assert.equal(giveUp(null, null, { run_id: "RUN-1", message_id: "M1" }, null).log.dropped, 1);
	// the countdown that is running belongs to this very message
	const same = giveUp(
		{ messageId: "M1" },
		{ messageId: "M1" },
		{ run_id: "RUN-1", message_id: "M1" },
		CAL
	);
	assert.equal(same.log.dropped, 1);
});

test("run:end of a message WITHOUT a countdown never cancels another message's running countdown", () => {
	const p = { run_id: "RUN-2", message_id: "M2" };
	const other = giveUp({ messageId: "M1" }, { page: "x", messageId: "M1" }, p, null);
	assert.equal(other.log.dropped, 0, "dropGotoMorph (which cancels) is not called");
	assert.equal(other.gotoMorph.value.messageId, "M1", "the running countdown keeps its latch");
	// ...but a latch that belongs to nobody running is cleared
	const stale = giveUp({ messageId: "M1" }, { page: "x", messageId: "M3" }, p, null);
	assert.equal(stale.log.dropped, 0);
	assert.equal(stale.gotoMorph.value, null);
	// and this run still gives up its own ownership
	const calendar = giveUp({ messageId: "M1" }, null, p, CAL);
	assert.equal(calendar.ownRuns.has("RUN-2"), false);
	assert.equal(calendar.ownRuns.has("RUN-1"), true);
});

// ---- Esc -------------------------------------------------------------------

function keyScope(over = {}) {
	const log = { stay: [], mic: 0, nudge: 0, artifact: 0 };
	const scope = {
		log,
		gotoCdState: { value: state() },
		gotoCd: { state: () => state() },
		store: { settingsOpen: false },
		modalIsOpen: () => false,
		stayInChat: (...a) => log.stay.push(a),
		gotoCardHasFocus: () => false,
		micState: { value: "idle" },
		nudge: { value: null },
		artifact: { value: null },
		cancelMic: () => log.mic++,
		cancelNudgeMic: () => log.nudge++,
		closeArtifact: () => log.artifact++,
		document: {},
		...over,
	};
	return scope;
}
const key = (over = {}) => ({
	key: "Escape",
	defaultPrevented: false,
	isComposing: false,
	...over,
});
const loadKey = (scope) => load(["function onGlobalKey(e)"], ["onGlobalKey"], scope).onGlobalKey;

test("Esc with a running countdown stays in chat and does not touch the mic or the artifact", () => {
	const scope = keyScope({
		micState: { value: "recording" },
		artifact: { value: { title: "x" } },
		gotoCardHasFocus: () => true,
	});
	loadKey(scope)(key());
	assert.deepEqual(scope.log.stay, [[true, "esc"]]);
	assert.equal(scope.log.mic + scope.log.artifact, 0);
});

test("Esc is left alone when a dialog is open, settings are open, IME is composing, or it was handled", () => {
	const cases = [
		[{ modalIsOpen: () => true }, key()],
		[{ store: { settingsOpen: true } }, key()],
		[{}, key({ isComposing: true })],
		[{}, key({ defaultPrevented: true })],
		[{ gotoCdState: { value: null } }, key()],
		// the mirror says "counting" but the machine says it is not: Esc must not be swallowed
		[{ gotoCd: { state: () => null } }, key()],
		[{}, key({ key: "Enter" })],
	];
	for (const [over, e] of cases) {
		const scope = keyScope(over);
		loadKey(scope)(e);
		assert.deepEqual(scope.log.stay, [], JSON.stringify([over, e]));
	}
});

test("the Esc branch is the first thing onGlobalKey does, guarded as the plan says, with no preventDefault", () => {
	const body = fnBody("function onGlobalKey(e)");
	const first = body
		.slice(body.indexOf("{") + 1)
		.replace(/^(\s*\/\/[^\n]*\n)*/, "")
		.trimStart();
	assert.ok(
		first.startsWith(
			'if (\n\t\te.key === "Escape" &&\n\t\tgotoCdState.value &&\n\t\tgotoCd.state() &&'
		),
		first.slice(0, 120)
	);
	const branch = body.slice(0, body.indexOf("return;\n\t}") + 10);
	for (const guard of [
		"gotoCd.state()",
		"!e.defaultPrevented",
		"!e.isComposing",
		"!store.settingsOpen",
		"!modalIsOpen(document)",
	])
		assert.ok(branch.includes(guard), guard);
	assert.doesNotMatch(branch, /preventDefault\(\)|stopPropagation\(\)/);
	assert.match(branch, /stayInChat\(gotoCardHasFocus\(\), "esc"\);\s*return;/);
});

test("after an exit, the card state is clear and Esc reaches the mic again", () => {
	const scope = keyScope({ micState: { value: "recording" } });
	scope.gotoCdState.value = state();
	// settleGoto clears the mirror; the machine is idle after any exit
	Object.assign(scope, exitScope({ gotoCdState: scope.gotoCdState }));
	scope.gotoCd = { state: () => null };
	scope.micState = { value: "recording" };
	settle(scope)(state(), "esc");
	loadKey(scope)(key());
	assert.equal(scope.log.mic, 1);
	assert.deepEqual(scope.log.stay, []);
});

// ---- Stay and focus --------------------------------------------------------

test("Stay cancels, and focus moves to the card's Open button only when asked", async () => {
	for (const [focus, want] of [
		[true, ["M1"]],
		[false, []],
	]) {
		const focused = [];
		const cancels = [];
		const scope = {
			gotoCd: { state: () => ({ messageId: "M1" }), cancel: (r) => cancels.push(r) },
			nextTick: (f) => Promise.resolve().then(f),
			gotoCardRefs: new Map([["M1", { focusOpen: () => focused.push("M1") }]]),
		};
		const { stayInChat } = load(
			['function stayInChat(focusCard, reason = "stay")'],
			["stayInChat"],
			scope
		);
		stayInChat(focus);
		await tick();
		assert.deepEqual(cancels, ["stay"]);
		assert.deepEqual(focused, want);
	}
});

// A keyboard activation of Stay returns focus to the card's Open button (the button Stay
// replaced in the tab order); a pointer click hands it to the composer.
test("the Stay button: a keyboard activation focuses Open, a pointer click focuses the composer", async () => {
	for (const [event, want] of [
		[{ detail: 0 }, { stay: [true], composer: 0 }],
		[{ detail: 1 }, { stay: [false], composer: 1 }],
		[{ detail: 2 }, { stay: [false], composer: 1 }],
		[undefined, { stay: [false], composer: 1 }],
	]) {
		const got = { stay: [], composer: 0 };
		const scope = {
			stayInChat: (focusCard) => got.stay.push(focusCard),
			nextTick: (f) => Promise.resolve().then(f),
			composerRef: { value: { focusInput: () => got.composer++ } },
		};
		load(["function onGotoStay(event)"], ["onGotoStay"], scope).onGotoStay(event);
		await tick();
		assert.deepEqual(got, want, JSON.stringify(event));
	}
	assert.match(src, /@stay="onGotoStay"/);
});

test("gotoCardHasFocus is true only when focus is inside the active countdown's card", () => {
	const inside = { contains: (n) => n === "ACTIVE" };
	for (const [id, active, want] of [
		["M1", "ACTIVE", true],
		["M1", "OTHER", false],
		[undefined, "ACTIVE", false],
	]) {
		const scope = {
			gotoCd: { state: () => (id ? { messageId: id } : null) },
			gotoCardRefs: new Map([["M1", { $el: inside }]]),
			document: { activeElement: active },
		};
		const { gotoCardHasFocus } = load(
			["function gotoCardHasFocus()"],
			["gotoCardHasFocus"],
			scope
		);
		assert.equal(gotoCardHasFocus(), want, `${id}/${active}`);
	}
});

// ---- pause / resume forwarding ---------------------------------------------

test("pause/resume are forwarded only from the card whose message holds the active countdown", () => {
	const calls = [];
	let active = "M2";
	const scope = {
		gotoCd: {
			state: () => (active ? { messageId: active } : null),
			pause: (r) => calls.push(["pause", r]),
			resume: (r) => calls.push(["resume", r]),
		},
	};
	const { onGotoCardPause, onGotoCardResume } = load(
		["function onGotoCardPause(m, reason)", "function onGotoCardResume(m, reason)"],
		["onGotoCardPause", "onGotoCardResume"],
		scope
	);
	onGotoCardPause({ name: "M1" }, "hover"); // an older card
	onGotoCardResume({ name: "M1" }, "hover"); // its late resume must not touch M2's countdown
	assert.deepEqual(calls, []);
	onGotoCardPause({ name: "M2" }, "hover");
	onGotoCardResume({ name: "M2" }, "hover");
	assert.deepEqual(calls, [
		["pause", "hover"],
		["resume", "hover"],
	]);
	active = null;
	onGotoCardResume({ name: "M2" }, "focus");
	assert.equal(calls.length, 2, "no countdown, nothing to forward");
	assert.match(src, /@pause="onGotoCardPause\(m, \$event\)"/);
	assert.match(src, /@resume="onGotoCardResume\(m, \$event\)"/);
});

// ---- visibility ------------------------------------------------------------

test("a tab that goes hidden pauses the countdown; coming back resumes it (one listener)", () => {
	for (const [vis, want] of [
		["hidden", [["pause", PAUSE.HIDDEN], ["flush"]]],
		["visible", [["resume", PAUSE.HIDDEN], ["resync"]]],
	]) {
		const calls = [];
		const scope = {
			document: { visibilityState: vis },
			gotoCd: {
				pause: (r) => calls.push(["pause", r]),
				resume: (r) => calls.push(["resume", r]),
			},
			onResync: () => calls.push(["resync"]),
			flushReveal: () => calls.push(["flush"]),
			PAUSE,
		};
		load(["function onVisibility()"], ["onVisibility"], scope).onVisibility();
		assert.deepEqual(calls, want, vis);
	}
	assert.equal((src.match(/addEventListener\("visibilitychange"/g) || []).length, 1);
});

test("the visibilitychange listener is removed on unmount, once for the one it adds", () => {
	const add =
		src.match(/document\.addEventListener\("visibilitychange", onVisibility\);/g) || [];
	const remove =
		src.match(/document\.removeEventListener\("visibilitychange", onVisibility\);/g) || [];
	assert.equal(add.length, 1);
	assert.equal(remove.length, 1);
	// the removal sits in an unmount hook
	const at = src.indexOf('document.removeEventListener("visibilitychange", onVisibility);');
	assert.match(src.slice(src.lastIndexOf("onUnmounted(", at), at), /^onUnmounted\(/);
});

// ---- the other cancel paths (source) ---------------------------------------

test("send() cancels the countdown synchronously: right after the booting guard, before any await", () => {
	const body = fnBody("async function send(textArg, resendAck)");
	const statements = body
		.split("\n")
		.filter((line) => line.trim() && !line.trim().startsWith("//"));
	// readiness.spec.js pins "if (booting.value) return;" as the first statement: nothing
	// (not even a cancel) runs before it
	assert.equal(statements[1].trim(), "if (booting.value) return;");
	assert.equal(statements[2].trim(), 'gotoCd.cancel("send");');
	assert.ok(statements.findIndex((l) => /\bawait\b/.test(l)) > 2, "no await before the cancel");
});

test("typing in the composer cancels the countdown, as onInput's first statement", () => {
	assert.match(src, /function onInput\(\) \{\n\tgotoCd\.cancel\("typing"\);[^\n]*\n/);
});

test("a new turn, a chat switch and an unmount cancel through dropGotoMorph; no hold timer remains", () => {
	const drop = fnBody('function dropGotoMorph(reason = "switch")');
	assert.match(drop, /gotoMorph\.value = null;/);
	assert.match(drop, /gotoCd\.cancel\(reason\);/);
	assert.match(src, /onUnmounted\(\(\) => dropGotoMorph\("unmount"\)\);/);
	assert.doesNotMatch(src, /GOTO_MORPH_HOLD_MS|gotoNavTimer/);
});

// M2: the chat-switch cancel. resetRunState runs on every conversation switch and new chat;
// without this call a countdown (and the latch) would follow the user into the next chat.
test("resetRunState cancels the countdown and drops the latch of the chat being left", () => {
	const body = fnBody("function resetRunState()");
	assert.match(body, /^\s*dropGotoMorph\(\);/m);
	// it runs for real: the call is a plain top-level statement, not behind a condition
	const line = body.split("\n").find((l) => l.includes("dropGotoMorph()"));
	assert.ok(/^\tdropGotoMorph\(\);/.test(line), line);
	// and a dropGotoMorph() with no argument cancels as a "switch"
	assert.match(fnBody('function dropGotoMorph(reason = "switch")'), /gotoCd\.cancel\(reason\)/);
});

test("the other turn-ending paths drop the latch too: a new run, an error, a stop", () => {
	assert.match(src, /case "run:start":[\s\S]*?dropGotoMorph\(\);[\s\S]*?case "queue:position"/);
	assert.match(src, /dropGotoMorph\(\); \/\/ an errored turn[^\n]*/);
	assert.match(src, /dropGotoMorph\(\); \/\/ a stopped turn[^\n]*/);
});

test("imports: the countdown, the card, the copy and the failure check", () => {
	assert.match(src, /import GotoCard from "@\/components\/chat\/GotoCard\.vue";/);
	assert.match(
		src,
		/import \{ createGotoCountdown, GOTO_COUNTDOWN_MS, PAUSE \} from "@\/lib\/gotoCountdown";/
	);
	assert.match(
		src,
		/import \{\s*useRoute,\s*useRouter,\s*onBeforeRouteLeave,\s*isNavigationFailure,\s*NavigationFailureType,?\s*\} from "vue-router";/
	);
});

test("the countdown clock is monotonic: performance.now(), not Date.now()", () => {
	const at = src.indexOf("const gotoCd = createGotoCountdown({");
	const cfg = src.slice(at, src.indexOf("\n});", at));
	assert.match(cfg, /now: \(\) => performance\.now\(\),/);
	assert.doesNotMatch(cfg, /Date\.now/);
});

test("the template feeds the countdown copy only to the card that holds it", () => {
	const fn = fnBody("function gotoCountdownOf(m)");
	const run = (s, m) =>
		new Function("gotoCdState", "gotoCountdownCopy", fn + "\n}\nreturn gotoCountdownOf;")(
			{ value: s },
			gotoCountdownCopy
		)(m);
	assert.equal(run(null, { name: "M1" }), null);
	assert.equal(run(state({ messageId: "M2" }), { name: "M1" }), null, "an older card gets none");
	const mine = run(state({ page: PAYMENT_CALENDAR_PAGE, secondsLeft: 2, paused: true }), {
		name: "M1",
	});
	assert.equal(mine.visible, "Paused · opening Payment Calendar in 2… Move away to continue.");
	assert.match(src, /@open-now="gotoCd\.openNow\(\)"/);
});

test("the template binds the card's ref by message id, and feeds the Opening text", () => {
	assert.ok(
		src.includes(':ref="(el) => setGotoCardRef(m.name, el)"'),
		"the card registers itself under its message id"
	);
	assert.match(src, /:opening="gotoOpeningOf\(m\)"/);
	// the registry behind that binding: set on mount, deleted on unmount
	const reg = new Map();
	const { setGotoCardRef } = load(["function setGotoCardRef(id, el)"], ["setGotoCardRef"], {
		gotoCardRefs: reg,
	});
	const el = {};
	setGotoCardRef("M1", el);
	assert.equal(reg.get("M1"), el);
	setGotoCardRef("M1", null);
	assert.equal(reg.has("M1"), false);
});

test("gotoOpeningOf: the Opening text only for the message that is opening", () => {
	const fn = fnBody("function gotoOpeningOf(m)");
	const run = (opening, m) =>
		new Function("gotoOpening", "gotoCountdownCopy", fn + "\n}\nreturn gotoOpeningOf;")(
			{ value: opening },
			gotoCountdownCopy
		)(m);
	assert.equal(run(null, { name: "M1" }), "");
	assert.equal(run({ messageId: "M2", page: DASHBOARDS_PAGE }, { name: "M1" }), "");
	assert.equal(
		run({ messageId: "M1", page: PAYMENT_CALENDAR_PAGE }, { name: "M1" }),
		"Opening Payment Calendar\u2026"
	);
});

test("the Dashboards push is caught like the calendar's, and still returned", () => {
	const make = (messageActionIsCurrent = true) => {
		const log = { caught: 0, pushed: [] };
		const promise = { catch: () => log.caught++ };
		const scope = {
			captureChatView: () => ({}),
			messageActionIsCurrent: () => messageActionIsCurrent,
			builderSeed: () => "seed",
			requestBefore: () => "",
			messages: { value: [] },
			gotoFiredKey: (id) => `k:${id}`,
			claimGotoFire: () => ({ build: false }),
			readGotoStamp: () => null,
			writeGotoStamp: () => {},
			setDashboardPrefill: () => {},
			router: { push: (to) => (log.pushed.push(to), promise) },
		};
		const { gotoDashboards } = load(
			["function gotoDashboards(prompt, messageId)"],
			["gotoDashboards"],
			scope
		);
		return { log, promise, gotoDashboards };
	};
	const ok = make();
	assert.equal(ok.gotoDashboards("p", "M1"), ok.promise);
	assert.deepEqual(ok.log.pushed, ["/dashboards"]);
	assert.equal(ok.log.caught, 1, "a rejected push cannot become an unhandled rejection");
	const moved = make(false);
	assert.equal(moved.gotoDashboards("p", "M1"), undefined);
	assert.deepEqual(moved.log.pushed, []);
});

test("the diagram card's Save as macro button joins the focus-visible rule; GotoCard owns its own", () => {
	const at = src.indexOf("/* visible keyboard focus (UX #15) */");
	assert.notEqual(at, -1);
	const rule = src.slice(at, src.indexOf("}", at));
	assert.match(rule, /\.jv-macrocard-btn:focus-visible/);
	assert.match(rule, /outline: 2px solid var\(--cta\);/);
	assert.match(card, /\.jv-gcard-btn:focus-visible/);
});
