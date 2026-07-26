// JF-018: the PWA's half of the Relay-Pump event fence.
//
// This is a test with no sibling module on purpose. The fence itself lives in
// jarvis/public/js/shared/pump_fence.mjs (shared with the Desk widget, which cannot
// import from a Vue app) and has its own exhaustive suite there, including a
// decision-parity walk against the desktop SPA's copy. What is UNTESTABLE there is
// the half that lives in this app: whether ChatView's onEvent actually routes its
// frames through the fence. The PWA has no component harness — no vitest, no
// @vue/test-utils, and mounting ChatView would need a router, a socket and the whole
// api surface — so the wiring is asserted against the SOURCE instead. Crude, but it
// is a real regression guard: it fails the moment the gate is removed, moved after
// the switch, or a new pump-sequenced kind is handled without being fenced.
//
// It runs under the PWA's own `npm test` (node --test src/lib/*.test.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	FENCED_KINDS,
	TERMINAL_KINDS,
	admitEvent,
	createFence,
} from "../../../jarvis/public/js/shared/pump_fence.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHAT_VIEW = path.join(HERE, "..", "views", "ChatView.vue");
const src = fs.readFileSync(CHAT_VIEW, "utf8");

// Kinds ChatView handles that are NOT pump-sequenced projections of the reply, and
// so must keep bypassing the fence (desktop does the same). Adding a kind here is a
// deliberate act; forgetting to is what the switch-coverage test below catches.
const UNFENCED_IN_CHATVIEW = new Set([
	"action:pending", // a parked write — losing it strands the approval
	"canvas",
	"conversation:renamed",
]);

function onEventBody() {
	const start = src.indexOf("function onEvent(p) {");
	assert.notEqual(start, -1, "ChatView must still define onEvent(p)");
	const end = src.indexOf("\nfunction ", start + 1);
	assert.notEqual(end, -1, "could not find the end of onEvent");
	return src.slice(start, end);
}

test("ChatView imports the SHARED fence, not a local re-implementation", () => {
	assert.match(
		src,
		/import \{ admitEvent, createFence \} from "@jsshared\/pump_fence\.mjs";/,
		"the fence must come from jarvis/public/js/shared so the widget and the PWA cannot drift"
	);
	assert.match(src, /createFence\(\)/, "ChatView must hold a fence instance");
});

test("onEvent gates on the fence BEFORE the switch, and returns on a drop", () => {
	const body = onEventBody();
	const gate = body.indexOf("if (!admitEvent(eventFence, p)) return;");
	const dispatch = body.indexOf("switch (p.kind) {");
	assert.notEqual(gate, -1, "the fence gate is missing from onEvent");
	assert.notEqual(dispatch, -1, "onEvent must still dispatch on p.kind");
	assert.ok(gate < dispatch, "the gate must run BEFORE any case body mutates state");
	// The conversation filter still has to come first — fencing frames for other
	// chats would poison this view's watermarks with another turn's sequence.
	const convFilter = body.indexOf("if (conv !== convId.value) return;");
	assert.ok(
		convFilter !== -1 && convFilter < gate,
		"the conversation filter must precede the gate"
	);
});

test("every pump-sequenced kind ChatView handles is covered by the fence", () => {
	const body = onEventBody();
	const kinds = [...body.matchAll(/case "([a-z:]+)":/g)].map((m) => m[1]);
	assert.ok(
		kinds.length >= 8,
		`expected ChatView's switch to still have cases, found ${kinds.length}`
	);
	for (const kind of kinds) {
		assert.ok(
			FENCED_KINDS.has(kind) || UNFENCED_IN_CHATVIEW.has(kind),
			`"${kind}" is handled by ChatView but is neither fenced nor a declared bypass — ` +
				"decide which it is (add it to FENCED_KINDS in pump_fence.mjs, or to " +
				"UNFENCED_IN_CHATVIEW here with the reason)"
		);
	}
	// The two teardown kinds must be the fence's terminals, or a stale one re-closes
	// a turn that has already been taken over.
	for (const terminal of ["run:end", "run:error"]) {
		assert.ok(kinds.includes(terminal), `ChatView must still handle ${terminal}`);
		assert.ok(TERMINAL_KINDS.has(terminal), `${terminal} must latch the fence`);
	}
});

// The two defects the fence exists to stop, expressed against the shared module
// with the PWA's own frame shapes.
test("a superseded pump's cumulative delta is dropped (it would REWIND the reply)", () => {
	const fence = createFence();
	const f = (kind, epoch, seq) => ({
		kind,
		conversation_id: "c1",
		run_id: "r1",
		pump_epoch: epoch,
		event_seq: seq,
	});
	assert.equal(admitEvent(fence, f("run:start", 2, 1)), true);
	assert.equal(admitEvent(fence, f("assistant:delta", 2, 2)), true);
	assert.equal(admitEvent(fence, f("assistant:delta", 1, 50)), false, "E-1 straggler dropped");
	assert.equal(admitEvent(fence, f("assistant:delta", 2, 2)), false, "replay dropped");
	assert.equal(admitEvent(fence, f("assistant:delta", 2, 3)), true, "real progress still flows");
});

test("a stale terminal is dropped (it would clear live + fire a redundant load())", () => {
	const fence = createFence();
	const f = (kind, epoch, seq) => ({
		kind,
		conversation_id: "c1",
		run_id: "r1",
		pump_epoch: epoch,
		event_seq: seq,
	});
	admitEvent(fence, f("assistant:delta", 3, 4));
	assert.equal(admitEvent(fence, f("run:end", 3, 4)), true, "the first terminal must settle");
	assert.equal(admitEvent(fence, f("run:end", 3, 4)), false, "the backstop repeat is one-shot");
	assert.equal(
		admitEvent(fence, f("run:error", 2, 9)),
		false,
		"a losing pump's error is dropped"
	);
	// A genuine takeover at E+1 still gets to stream and settle.
	assert.equal(admitEvent(fence, f("assistant:delta", 4, 1)), true);
	assert.equal(admitEvent(fence, f("run:end", 4, 1)), true);
});

test("tool frames are fenced too — they carry no message_id to fall back on", () => {
	const fence = createFence();
	const f = (kind, epoch, seq) => ({ kind, run_id: "r1", pump_epoch: epoch, event_seq: seq });
	admitEvent(fence, f("tool:start", 5, 2));
	assert.equal(admitEvent(fence, f("tool:end", 4, 8)), false);
	assert.equal(admitEvent(fence, f("tool:end", 5, 3)), true);
});

test("approvals are never fenced away", () => {
	const fence = createFence();
	admitEvent(fence, { kind: "assistant:delta", run_id: "r1", pump_epoch: 9, event_seq: 9 });
	assert.equal(
		admitEvent(fence, { kind: "action:pending", run_id: "r1", token: "t1", pump_epoch: 1 }),
		true,
		"a parked write must reach the UI whatever the pump did"
	);
});
