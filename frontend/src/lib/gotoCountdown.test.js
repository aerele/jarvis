// The hand-off countdown state machine (gotoCountdown.js), driven by an injected
// fake clock: no real timers, so every deadline below is exact. ChatView only wires
// this up; what must hold is here (never a late or double open, no interval left
// behind, pause that cannot be starved of time).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	createGotoCountdown,
	GOTO_COUNTDOWN_MS,
	PAUSE,
	RESUME_FLOOR_MS,
} from "./gotoCountdown.js";

// A clock whose intervals fire only as `advance` moves time past them.
function fakeClock() {
	let t = 0;
	let nextId = 0;
	const timers = new Map();
	return {
		now: () => t,
		setInterval(fn, ms) {
			timers.set(++nextId, { fn, ms, next: t + ms });
			return nextId;
		},
		clearInterval(id) {
			timers.delete(id);
		},
		count: () => timers.size,
		intervals: () => [...timers.values()].map((x) => x.ms),
		// The clock leaps (a suspended laptop, a frozen tab) and each interval fires ONCE after it.
		jump(ms) {
			t += ms;
			for (const timer of timers.values()) {
				timer.next = t + timer.ms;
				timer.fn();
			}
		},
		advance(ms) {
			const end = t + ms;
			for (;;) {
				let at = Infinity;
				let id = 0;
				for (const [k, v] of timers) if (v.next < at) [at, id] = [v.next, k];
				if (at > end) break;
				t = at;
				const timer = timers.get(id);
				timer.next += timer.ms;
				timer.fn();
			}
			t = end;
		},
		at(ms) {
			this.advance(ms - t);
		},
	};
}

const GOTO = {
	messageId: "m1",
	runId: "r1",
	page: "payment-calendar",
	action: "payment-calendar",
};

function make(overrides = {}) {
	const clock = fakeClock();
	const log = { ticks: [], expires: [], opens: [], cancels: [], insideState: [] };
	let cd;
	const deps = {
		durationMs: GOTO_COUNTDOWN_MS,
		now: clock.now,
		setInterval: clock.setInterval,
		clearInterval: clock.clearInterval,
		handled: new Set(),
		onTick: (s) => log.ticks.push({ ...s }),
		onExpire: (s) => {
			log.insideState.push(cd.state());
			log.expires.push({ ...s, at: clock.now() });
		},
		onOpenNow: (s) => {
			log.insideState.push(cd.state());
			log.opens.push({ ...s, at: clock.now() });
		},
		onCancel: (s, reason) => {
			log.insideState.push(cd.state());
			log.cancels.push({ ...s, reason });
		},
		...overrides,
	};
	cd = createGotoCountdown(deps);
	return { cd, clock, log, deps };
}

test("the constants: 3 s countdown, 2 s resume floor", () => {
	assert.equal(GOTO_COUNTDOWN_MS, 3000);
	assert.equal(RESUME_FLOOR_MS, 2000);
});

test("ticks 3, 2, 1, then expires exactly once at 3000 ms and never reports 0", () => {
	const { cd, clock, log } = make();
	assert.equal(cd.start(GOTO), true);
	assert.deepEqual(cd.state(), { ...GOTO, secondsLeft: 3, paused: false });
	clock.at(2999);
	assert.equal(log.expires.length, 0);
	clock.at(3000);
	clock.advance(10000);
	assert.deepEqual(
		log.ticks.map((t) => t.secondsLeft),
		[3, 2, 1]
	);
	assert.ok(log.ticks.every((t) => t.secondsLeft >= 1 && t.paused === false));
	assert.equal(log.expires.length, 1);
	assert.equal(log.expires[0].at, 3000);
	assert.equal(log.opens.length + log.cancels.length, 0);
});

test("onTick fires only when the seconds or the paused flag change", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(500);
	clock.at(900);
	assert.equal(log.ticks.length, 1);
	clock.at(1000); // 2000 ms left -> 2 s
	assert.deepEqual(
		log.ticks.map((t) => t.secondsLeft),
		[3, 2]
	);
});

test("the tick interval is 250 ms: one interval, and it is cleared with the countdown", () => {
	const { cd, clock } = make();
	cd.start(GOTO);
	assert.deepEqual(clock.intervals(), [250]);
	cd.cancel("stay");
	assert.deepEqual(clock.intervals(), []);
});

test("the duration defaults to GOTO_COUNTDOWN_MS when deps.durationMs is not given", () => {
	const { durationMs: _omit, ...rest } = make().deps;
	const clock = fakeClock();
	const expired = [];
	const cd = createGotoCountdown({
		...rest,
		now: clock.now,
		setInterval: clock.setInterval,
		clearInterval: clock.clearInterval,
		onExpire: (s) => expired.push(clock.now()),
	});
	cd.start(GOTO);
	clock.at(2999);
	assert.deepEqual(expired, []);
	clock.at(3000);
	assert.deepEqual(expired, [3000]);
});

test("PAUSE names the three pause reasons the card and the view share", () => {
	assert.deepEqual(PAUSE, { HOVER: "hover", FOCUS: "focus", HIDDEN: "hidden" });
	const { cd, clock, log } = make();
	cd.start(GOTO);
	cd.pause(PAUSE.HOVER);
	cd.pause(PAUSE.FOCUS);
	cd.resume(PAUSE.HOVER);
	assert.equal(cd.state().paused, true);
	cd.resume(PAUSE.FOCUS);
	assert.equal(cd.state().paused, false);
	clock.at(10000);
	assert.equal(log.expires.length, 1);
});

test("the duration comes from deps.durationMs", () => {
	const { cd, clock, log } = make({ durationMs: 1000 });
	cd.start(GOTO);
	assert.equal(cd.state().secondsLeft, 1);
	clock.at(999);
	assert.equal(log.expires.length, 0);
	clock.at(1000);
	assert.equal(log.expires.length, 1);
});

test("pause freezes the remaining time; resume with 2 s or more left gives exactly that", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(1000);
	cd.pause("hover");
	assert.equal(cd.state().paused, true);
	clock.at(6000);
	assert.equal(log.expires.length, 0);
	cd.resume("hover");
	assert.equal(cd.state().paused, false);
	clock.at(7999);
	assert.equal(log.expires.length, 0);
	clock.at(8000);
	assert.equal(log.expires.length, 1);
	assert.equal(log.expires[0].at, 8000);
});

test("resume floor: 200 ms left at pause becomes 2000 ms after resume", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(2800);
	cd.pause("focus");
	clock.at(4000);
	cd.resume("focus");
	assert.equal(cd.state().secondsLeft, 2);
	clock.at(5999);
	assert.equal(log.expires.length, 0);
	clock.at(6000);
	assert.equal(log.expires.length, 1);
	assert.equal(log.expires[0].at, 6000);
});

test("onTick reports paused:true on pause and paused:false on resume", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(500);
	cd.pause("hover");
	assert.deepEqual(log.ticks.at(-1), { ...GOTO, secondsLeft: 3, paused: true });
	cd.resume("hover");
	assert.equal(log.ticks.at(-1).paused, false);
	assert.equal(log.ticks.at(-1).secondsLeft, 3);
});

test("paused while ANY reason is held: resuming one of two stays paused", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	cd.pause("hover");
	cd.pause("hidden");
	cd.resume("hover");
	assert.equal(cd.state().paused, true);
	clock.at(20000);
	assert.equal(log.expires.length, 0);
	cd.resume("hidden");
	assert.equal(cd.state().paused, false);
	clock.at(22999);
	assert.equal(log.expires.length, 0);
	clock.at(23000); // the full 3000 ms were still left
	assert.equal(log.expires.length, 1);
});

test("two pauses with time between them keep the first freeze: the second reason does not re-measure", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(500);
	cd.pause("hover"); // 2500 ms left, frozen
	clock.at(1500);
	cd.pause("focus"); // one second later: still 2500 ms, not 1500
	cd.resume("hover");
	clock.at(4000);
	cd.resume("focus");
	assert.equal(log.expires.length, 0);
	clock.at(6499);
	assert.equal(log.expires.length, 0);
	clock.at(6500); // 4000 + the 2500 ms that were left
	assert.equal(log.expires.length, 1);
});

test("holding the same reason twice is one reason", () => {
	const { cd } = make();
	cd.start(GOTO);
	cd.pause("hover");
	cd.pause("hover");
	cd.resume("hover");
	assert.equal(cd.state().paused, false);
});

test("resume of a reason not held is a no-op: it neither unpauses nor applies the floor", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(2800);
	cd.pause("hover");
	clock.at(3500);
	cd.resume("focus"); // never held
	assert.equal(cd.state().paused, true);
	clock.at(10000);
	assert.equal(log.expires.length, 0);
	// and while running, a stray resume must not stretch the deadline
	const b = make();
	b.cd.start(GOTO);
	b.clock.at(2800);
	b.cd.resume("hover");
	b.clock.at(3000);
	assert.equal(b.log.expires.length, 1);
	assert.equal(b.log.expires[0].at, 3000);
});

// A suspended machine or a frozen tab: the interval fires once, long after the deadline. That
// must not open the page the user never saw counting down.
test("a stalled tick (the clock leapt) never expires: it re-arms with the time the user last saw", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(500); // 2500 ms left at the last tick
	clock.jump(5000); // now 5500, long past the 3000 deadline
	assert.equal(log.expires.length, 0, "no expiry on the stalled tick");
	assert.equal(cd.state().paused, false);
	clock.at(7999);
	assert.equal(log.expires.length, 0);
	clock.at(8000); // 5500 + 2500
	assert.equal(log.expires.length, 1);
});

test("a stalled tick applies the resume floor when little time was left", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(2750); // 250 ms left
	clock.jump(5000); // 7750
	assert.equal(log.expires.length, 0);
	clock.at(9749);
	assert.equal(log.expires.length, 0);
	clock.at(9750); // 7750 + the 2000 ms floor
	assert.equal(log.expires.length, 1);
});

test("a gap of exactly 1000 ms between ticks is not a stall; one over is", () => {
	const a = make();
	a.cd.start(GOTO);
	a.clock.at(2000); // 1000 ms left
	a.clock.jump(1000); // gap 1000: past the deadline, expires normally
	assert.equal(a.log.expires.length, 1);
	const b = make();
	b.cd.start(GOTO);
	b.clock.at(2000);
	b.clock.jump(1001); // gap 1001: a stall
	assert.equal(b.log.expires.length, 0);
});

test("after a resume the stall guard measures from the resume, not from before the pause", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(500);
	cd.pause("hover");
	clock.at(20000); // a long pause: no ticks count
	cd.resume("hover"); // 2500 ms left again
	clock.advance(250); // the next tick is a normal one
	assert.equal(log.expires.length, 0);
	clock.at(22500);
	assert.equal(log.expires.length, 1);
});

test("pause and resume with no active countdown do nothing", () => {
	const { cd, log } = make();
	assert.doesNotThrow(() => {
		cd.pause("hover");
		cd.resume("hover");
		cd.cancel("stay");
		cd.openNow();
	});
	assert.equal(cd.state(), null);
	assert.deepEqual(log.ticks.concat(log.cancels, log.opens, log.expires), []);
});

test("a reason held before a start (no countdown) or before an exit never pauses the next start", () => {
	const { cd, clock, log } = make();
	cd.pause("hover"); // no countdown: ignored
	cd.start(GOTO);
	assert.equal(cd.state().paused, false);
	cd.pause("focus");
	cd.cancel("stay");
	cd.start({ ...GOTO, messageId: "m2" });
	assert.equal(cd.state().paused, false);
	cd.pause("hidden");
	clock.at(100);
	cd.openNow(); // exit while paused
	cd.start({ ...GOTO, messageId: "m3" });
	assert.equal(cd.state().paused, false);
	clock.advance(3000);
	assert.equal(log.expires.length, 1);
	assert.equal(log.expires[0].messageId, "m3");
});

test("start clears reasons held from a previous countdown it replaces", () => {
	const { cd } = make();
	cd.start(GOTO);
	cd.pause("hover");
	cd.start({ ...GOTO, messageId: "m2" });
	assert.equal(cd.state().paused, false);
	cd.resume("hover"); // the old reason is gone
	assert.equal(cd.state().paused, false);
});

test("starting while another countdown runs cancels it as 'switch' and starts the new one", () => {
	const { cd, log } = make();
	cd.start(GOTO);
	assert.equal(cd.start({ ...GOTO, messageId: "m2", runId: "r2" }), true);
	assert.equal(log.cancels.length, 1);
	assert.equal(log.cancels[0].reason, "switch");
	assert.equal(log.cancels[0].messageId, "m1");
	assert.equal(cd.state().messageId, "m2");
});

for (const reason of [
	"stay",
	"esc",
	"typing",
	"send",
	"switch",
	"unmount",
	"refused",
	"navfail",
]) {
	test(`cancel("${reason}") calls onCancel once with the reason, clears everything, and blocks a re-start`, () => {
		const { cd, clock, log } = make();
		cd.start(GOTO);
		clock.at(1000);
		cd.pause("hover");
		cd.cancel(reason);
		cd.cancel(reason);
		assert.equal(log.cancels.length, 1);
		assert.equal(log.cancels[0].reason, reason);
		assert.equal(log.cancels[0].messageId, "m1");
		assert.equal(log.cancels[0].runId, "r1");
		assert.equal(cd.state(), null);
		assert.equal(clock.count(), 0);
		clock.advance(20000);
		assert.equal(log.expires.length, 0);
		assert.equal(cd.isHandled("m1"), true);
		assert.equal(cd.start(GOTO), false);
		assert.equal(cd.state(), null);
		assert.equal(clock.count(), 0);
	});
}

test("expiry blocks a re-start for the same message", () => {
	const { cd, clock } = make();
	cd.start(GOTO);
	clock.at(3000);
	assert.equal(cd.start(GOTO), false);
	assert.equal(cd.isHandled("m1"), true);
});

test("openNow calls onOpenNow once (never onExpire), clears, and blocks a re-start", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(1200);
	cd.openNow();
	cd.openNow();
	clock.advance(20000);
	assert.equal(log.opens.length, 1);
	assert.equal(log.opens[0].at, 1200);
	assert.equal(log.opens[0].messageId, "m1");
	assert.equal(log.expires.length, 0);
	assert.equal(log.cancels.length, 0);
	assert.equal(clock.count(), 0);
	assert.equal(cd.start(GOTO), false);
});

test("openNow works while paused", () => {
	const { cd, log } = make();
	cd.start(GOTO);
	cd.pause("hover");
	cd.openNow();
	assert.equal(log.opens.length, 1);
});

test("state() is null inside onExpire, onOpenNow and onCancel, and after every exit", () => {
	const a = make();
	a.cd.start(GOTO);
	a.clock.at(3000);
	assert.deepEqual(a.log.insideState, [null]);
	assert.equal(a.cd.state(), null);

	const b = make();
	b.cd.start(GOTO);
	b.cd.openNow();
	assert.deepEqual(b.log.insideState, [null]);
	assert.equal(b.cd.state(), null);

	const c = make();
	c.cd.start(GOTO);
	c.cd.cancel("esc");
	assert.deepEqual(c.log.insideState, [null]);
	assert.equal(c.cd.state(), null);
});

test("the exit callbacks receive the snapshot of the exited countdown", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(3000);
	assert.deepEqual(
		{ ...log.expires[0], at: undefined },
		{ ...GOTO, secondsLeft: 1, paused: false, at: undefined }
	);
});

test("a callback may start the next countdown for another message", () => {
	let cd;
	const { clock, log, deps } = make();
	cd = createGotoCountdown({
		...deps,
		onExpire: () => cd.start({ ...GOTO, messageId: "m2" }),
	});
	cd.start(GOTO);
	clock.at(3000);
	assert.equal(cd.state().messageId, "m2");
	assert.equal(log.cancels.length, 0);
});

test("start() returns false and leaks no interval when the switch-cancel callback starts another countdown", () => {
	let cd;
	const { clock, deps } = make();
	cd = createGotoCountdown({
		...deps,
		onCancel: (_s, reason) => {
			if (reason === "switch") cd.start({ ...GOTO, messageId: "m3" });
		},
	});
	assert.equal(cd.start(GOTO), true);
	assert.equal(cd.start({ ...GOTO, messageId: "m2" }), false);
	assert.equal(cd.state().messageId, "m3", "the countdown the callback started stays");
	assert.equal(clock.count(), 1, "one interval, not two");
	cd.cancel("stay");
	assert.equal(clock.count(), 0);
});

test("no interval remains after any exit", () => {
	for (const exit of [
		(cd, clock) => clock.at(3000),
		(cd) => cd.cancel("stay"),
		(cd) => cd.cancel("refused"),
		(cd) => cd.cancel("navfail"),
		(cd) => cd.openNow(),
	]) {
		const { cd, clock } = make();
		cd.start(GOTO);
		assert.equal(clock.count(), 1);
		exit(cd, clock);
		assert.equal(clock.count(), 0);
	}
});

test("markHandled(id) makes start(id) false and leaves other messages alone", () => {
	const { cd, clock } = make();
	cd.markHandled("m1");
	assert.equal(cd.isHandled("m1"), true);
	assert.equal(cd.start(GOTO), false);
	assert.equal(cd.state(), null);
	assert.equal(clock.count(), 0);
	assert.equal(cd.isHandled("m2"), false);
	assert.equal(cd.start({ ...GOTO, messageId: "m2" }), true);
});

test("start for the message that is counting down is refused and does not restart it", () => {
	const { cd, clock, log } = make();
	cd.start(GOTO);
	clock.at(1000);
	assert.equal(cd.start(GOTO), false);
	assert.equal(cd.state().secondsLeft, 2);
	clock.at(3000);
	assert.equal(log.expires.length, 1);
});

test("the handled set holds at most 200 ids, oldest dropped", () => {
	const { cd, deps } = make();
	for (let i = 0; i < 205; i++) cd.markHandled(`id-${i}`);
	assert.equal(deps.handled.size, 200);
	assert.equal(cd.isHandled("id-0"), false);
	assert.equal(cd.isHandled("id-4"), false);
	assert.equal(cd.isHandled("id-5"), true);
	assert.equal(cd.isHandled("id-204"), true);
	// start() adds to the same capped set
	for (let i = 0; i < 205; i++) {
		cd.start({ ...GOTO, messageId: `s-${i}` });
		cd.cancel("stay");
	}
	assert.equal(deps.handled.size, 200);
	assert.equal(cd.isHandled("s-204"), true);
	assert.equal(cd.isHandled("s-0"), false);
});

test("markHandled on a known id refreshes its age: it outlives ids marked after it", () => {
	const { cd, deps } = make();
	for (let i = 0; i < 200; i++) cd.markHandled(`id-${i}`);
	cd.markHandled("id-0"); // now the newest
	cd.markHandled("id-200"); // pushes the oldest (now id-1) out
	assert.equal(deps.handled.size, 200);
	assert.equal(cd.isHandled("id-0"), true);
	assert.equal(cd.isHandled("id-1"), false);
});

test("by default the handled set is module-scope: a second instance (a ChatView remount) sees it", () => {
	const { deps } = make();
	const { handled: _omit, ...rest } = deps;
	const id = "shared-across-instances";
	const first = createGotoCountdown(rest);
	first.markHandled(id);
	const second = createGotoCountdown(rest);
	assert.equal(second.isHandled(id), true);
	assert.equal(second.start({ ...GOTO, messageId: id }), false);
});
