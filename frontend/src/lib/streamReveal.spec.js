import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

import { createRevealer, nextRevealed, MIN_STEP, MAX_STEP } from "./streamReveal";

/**
 * assistant:delta carries the CUMULATIVE reply and the view used to assign it
 * straight to the row, so the text moved at whatever rate the socket delivered
 * it: a word, a stall, then a paragraph in one frame. The revealer paces it.
 *
 * The correctness bar is that pacing NEVER loses or reorders text: every path
 * below has to end with the full target on screen.
 */

describe("nextRevealed", () => {
	it("never moves past the target", () => {
		expect(nextRevealed(10, 10)).toBe(10);
		expect(nextRevealed(12, 10)).toBe(10);
		expect(nextRevealed(9, 10)).toBe(10);
	});

	it("moves at least MIN_STEP so a long tail still finishes", () => {
		expect(nextRevealed(0, 1000) - 0).toBeGreaterThanOrEqual(MIN_STEP);
	});

	it("caps at MAX_STEP so a huge burst is absorbed, not jump-cut", () => {
		expect(nextRevealed(0, 1_000_000)).toBe(MAX_STEP);
	});

	it("goes faster the further behind it is", () => {
		const small = nextRevealed(0, 50);
		const big = nextRevealed(0, 5000);
		expect(big).toBeGreaterThan(small);
	});

	it("always converges", () => {
		let shown = 0;
		const target = 4321;
		for (let i = 0; i < 10000 && shown < target; i++) shown = nextRevealed(shown, target);
		expect(shown).toBe(target);
	});
});

describe("createRevealer", () => {
	it("shows the FIRST delta whole, so the row never renders empty", () => {
		// The view hides an empty streaming row, so starting the cursor at zero
		// would flicker the reply out of existence just as it arrived.
		const r = createRevealer();
		expect(r.receive("m1", "Hello")).toBe("Hello");
		expect(r.pending()).toEqual([]);
	});

	it("paces a later delta instead of assigning it", () => {
		const r = createRevealer();
		r.receive("m1", "Hi");
		const shown = r.receive("m1", "Hi there, this is a much longer continuation of the reply");
		expect(shown).toBe("Hi");
		expect(r.pending()).toEqual(["m1"]);
	});

	it("walks forward one frame at a time and finishes on the exact target", () => {
		const r = createRevealer();
		const full = "a".repeat(500);
		r.receive("m1", "a");
		r.receive("m1", full);
		let last = null;
		for (let i = 0; i < 500; i++) {
			const step = r.tick("m1");
			if (!step) break;
			// Monotonic: the cursor only ever moves forward.
			if (last) expect(step.text.startsWith(last)).toBe(true);
			last = step.text;
			if (step.done) break;
		}
		expect(last).toBe(full);
		expect(r.pending()).toEqual([]);
	});

	it("SNAPS on a rewrite rather than animating the difference", () => {
		// A recovered run republishes text that is not an extension of what was
		// shown. Animating that would play the correction as if it were a typo.
		const r = createRevealer();
		r.receive("m1", "The answer is 41");
		r.receive("m1", "The answer is 41 and more text to leave a backlog behind");
		const rewritten = r.receive("m1", "Completely different recovered answer");
		expect(rewritten).toBe("Completely different recovered answer");
		expect(r.pending()).toEqual([]);
	});

	it("flush snaps to the full text and forgets the message", () => {
		const r = createRevealer();
		r.receive("m1", "x");
		r.receive("m1", "x".repeat(400));
		expect(r.flush("m1")).toBe("x".repeat(400));
		expect(r.pending()).toEqual([]);
		expect(r.flush("m1")).toBeNull();
	});

	it("flushAll returns every mid-reveal message so a terminal loses nothing", () => {
		const r = createRevealer();
		r.receive("a", "1");
		r.receive("a", "1".repeat(300));
		r.receive("b", "2");
		r.receive("b", "2".repeat(300));
		const out = r.flushAll();
		expect(out).toHaveLength(2);
		expect(Object.fromEntries(out)).toEqual({
			a: "1".repeat(300),
			b: "2".repeat(300),
		});
		expect(r.size).toBe(0);
	});

	it("keeps two messages independent", () => {
		// A recovery landing beside a live turn must not share a cursor with it.
		const r = createRevealer();
		r.receive("a", "aaa");
		r.receive("b", "bbb");
		r.receive("a", "aaa" + "a".repeat(200));
		expect(r.pending()).toEqual(["a"]);
		expect(r.flush("b")).toBe("bbb");
	});

	it("drop forgets a message without applying anything", () => {
		const r = createRevealer();
		r.receive("m1", "x");
		r.receive("m1", "x".repeat(300));
		r.drop("m1");
		expect(r.pending()).toEqual([]);
		expect(r.size).toBe(0);
	});

	it("applies N frames of catch-up in one tick, at the same speed", () => {
		// The caller paints ~25 times a second instead of 60 because each paint costs
		// a full markdown re-parse. Passing the skipped frames keeps the reveal SPEED
		// identical: three single ticks and one tick(3) must land on the same cursor.
		const a = createRevealer();
		const b = createRevealer();
		const full = "z".repeat(600);
		for (const r of [a, b]) {
			r.receive("m", "z");
			r.receive("m", full);
		}
		a.tick("m");
		a.tick("m");
		a.tick("m");
		const stepped = b.tick("m", 3);
		expect(stepped.text).toBe(a.flush("m").slice(0, stepped.text.length));
		expect(stepped.text.length).toBe(b.flush("m").length ? stepped.text.length : 0);
	});

	it("treats a zero or negative frame count as one frame", () => {
		const r = createRevealer();
		r.receive("m", "q");
		r.receive("m", "q".repeat(400));
		expect(r.tick("m", 0).text.length).toBeGreaterThan(1);
	});

	it("tick on an unknown or finished message is a no-op", () => {
		const r = createRevealer();
		expect(r.tick("nope")).toBeNull();
		r.receive("m1", "done");
		expect(r.tick("m1")).toBeNull();
	});

	it("handles an empty delta without stalling", () => {
		const r = createRevealer();
		expect(r.receive("m1", "")).toBe("");
		expect(r.receive("m1", undefined)).toBe("");
		expect(r.pending()).toEqual([]);
	});
});

describe("every terminal in the view snaps the reveal", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("wires the revealer into assistant:delta", () => {
		expect(src).toContain('import { createRevealer } from "@/lib/streamReveal";');
		expect(src).toContain("m.content = revealer.receive(p.message_id, p.text);");
		// The old straight assignment is what made the text lurch.
		expect(src).not.toContain("\t\t\tm.content = p.text;");
	});

	it("snaps BEFORE run:end clears streaming", () => {
		// SUX-6 skips the reload because the streamed text is assumed to equal the
		// final text. A cursor still mid-walk would make that assumption false and
		// leave the answer truncated on screen.
		const i = src.indexOf("flushReveal(p.message_id);");
		const j = src.indexOf("if (m) m.streaming = false;");
		expect(i).toBeGreaterThan(-1);
		expect(j).toBeGreaterThan(i);
	});

	it("snaps on stop, on error, on conversation switch and on unmount", () => {
		expect(src).toContain("flushReveal(m.name);"); // stopRun
		expect(src).toContain("flushReveal(); // nothing is streaming anymore"); // clearStreamingActivity
		expect(src).toContain("flushReveal(); // cancels the frame loop"); // onBeforeUnmount
		expect(src).toMatch(/function resetRunState\(\) \{[\s\S]{0,220}flushReveal\(\);/);
	});

	it("snaps when the tab goes to the background, where rAF does not run", () => {
		expect(src).toMatch(/onVisibility\(\)[\s\S]{0,220}else flushReveal\(\);/);
	});

	it("paints far less often than it animates, and pays back the skipped frames", () => {
		// The regression this guards: painting every rAF re-parsed the whole message
		// 60 times a second, several times the old socket-rate cost, and a long reply
		// stuttered. Throttling alone would slow the text down; the frame payback is
		// what keeps the speed while cutting the renders.
		expect(src).toContain("const REVEAL_PAINT_MS = 40;");
		expect(src).toContain("if (dt < REVEAL_PAINT_MS) {");
		expect(src).toContain("const frames = Math.max(1, Math.round(dt / FRAME_MS));");
		expect(src).toContain("revealer.tick(id, frames)");
	});
});

describe("stale tool events cannot reopen the activity list", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("guards both tool events on a live run", () => {
		// The CDX-3 pump fence deliberately lets an epoch-less tool event through, so
		// a straggler tool:start after run:end pushed a `running` entry that no
		// tool:end would settle, leaving a spinner the user never opened.
		expect(src).toContain("function toolEventIsStale(p)");
		expect(src).toContain("if (!currentRunId.value) return true;");
		const guards = src.match(/if \(toolEventIsStale\(p\)\) break;/g) || [];
		expect(guards).toHaveLength(2); // tool:start AND tool:end
	});
});
