import { describe, it, expect, vi } from "vitest";
import {
	OP_PHASE,
	OP_STATE,
	isTerminal,
	classifyOperation,
	operationStore,
	createOperationController,
} from "./llmOperation.js";
import { statusOf, saveDescriptor, OP_IDS } from "./__fixtures__/llmOperation.fixtures.js";

// A deterministic virtual clock so the poll loop is fully controllable: setTimer
// enqueues, advance() fires everything due and flushes the async poll microtasks.
function fakeClock() {
	let t = 0;
	let seq = 0;
	const q = new Map(); // id -> {due, fn}
	const api = {
		now: () => t,
		setTimer: (fn, ms) => {
			const id = ++seq;
			q.set(id, { due: t + Math.max(0, ms || 0), fn });
			return id;
		},
		clearTimer: (id) => q.delete(id),
		async advance(ms) {
			const target = t + ms;
			// Fire due callbacks in time order, flushing promises between each, until
			// we reach the target time or run out of work.
			// A generous guard against runaway rescheduling.
			for (let guard = 0; guard < 10000; guard++) {
				let next = null;
				for (const [id, item] of q) {
					if (item.due <= target && (!next || item.due < next.item.due))
						next = { id, item };
				}
				if (!next) break;
				q.delete(next.id);
				t = Math.max(t, next.item.due);
				next.item.fn();
				// flush microtasks (the async poll body)
				await Promise.resolve();
				await Promise.resolve();
			}
			t = target;
		},
	};
	return api;
}

function memoryStorage() {
	const m = new Map();
	return {
		getItem: (k) => (m.has(k) ? m.get(k) : null),
		setItem: (k, v) => m.set(k, String(v)),
		removeItem: (k) => m.delete(k),
	};
}

describe("classifyOperation (pure projection)", () => {
	it("maps working states", () => {
		expect(classifyOperation(statusOf("pending")).phase).toBe(OP_PHASE.WORKING);
		expect(classifyOperation(statusOf("applying")).phase).toBe(OP_PHASE.WORKING);
	});

	it("applied_waiting_readiness is a first-class FINISHING phase, not a spinner", () => {
		const ui = classifyOperation(statusOf("applied_waiting_readiness"));
		expect(ui.phase).toBe(OP_PHASE.FINISHING);
		expect(ui.terminal).toBe(false);
		expect(ui.canNavigate).toBe(false);
		expect(ui.chatReadinessReason).toMatch(/plugin/i);
	});

	it("ready is the only phase that may navigate, and it is terminal", () => {
		const ui = classifyOperation(statusOf("ready"));
		expect(ui.phase).toBe(OP_PHASE.READY);
		expect(ui.terminal).toBe(true);
		expect(ui.canNavigate).toBe(true);
	});

	it("a transient failure is RETRY (retryable); a permanent rejection is REJECTED (not)", () => {
		const transient = classifyOperation(statusOf("failed"));
		expect(transient.phase).toBe(OP_PHASE.RETRY);
		expect(transient.retryable).toBe(true);
		expect(transient.terminal).toBe(true);

		const rejected = classifyOperation(statusOf("rejected"));
		expect(rejected.phase).toBe(OP_PHASE.REJECTED);
		expect(rejected.retryable).toBe(false);
		expect(rejected.canNavigate).toBe(false);
	});

	it("blocked-oauth is a retryable failure, not a permanent rejection", () => {
		const ui = classifyOperation(statusOf("blocked_oauth"));
		expect(ui.phase).toBe(OP_PHASE.RETRY);
		expect(ui.state).toBe(OP_STATE.FAILED);
	});

	it("superseded (newer save or authority change) is its own phase, never navigable", () => {
		expect(classifyOperation(statusOf("superseded")).phase).toBe(OP_PHASE.SUPERSEDED);
		expect(classifyOperation(statusOf("superseded_authority")).phase).toBe(
			OP_PHASE.SUPERSEDED
		);
		expect(classifyOperation(statusOf("superseded")).canNavigate).toBe(false);
	});

	it("carries the per-operation retry_after for a rate-limited status", () => {
		const ui = classifyOperation(statusOf("failed", { retry_after_seconds: 30 }));
		expect(ui.retryAfterSeconds).toBe(30);
	});

	it("a null/absent status never navigates", () => {
		const ui = classifyOperation(null);
		expect(ui.canNavigate).toBe(false);
		expect(ui.terminal).toBe(false);
	});
});

describe("isTerminal", () => {
	it("only ready/failed/superseded are terminal", () => {
		expect(isTerminal(OP_STATE.READY)).toBe(true);
		expect(isTerminal(OP_STATE.FAILED)).toBe(true);
		expect(isTerminal(OP_STATE.SUPERSEDED)).toBe(true);
		expect(isTerminal(OP_STATE.PENDING)).toBe(false);
		expect(isTerminal(OP_STATE.APPLYING)).toBe(false);
		expect(isTerminal(OP_STATE.APPLIED_WAITING_READINESS)).toBe(false);
	});
});

describe("operationStore (reload resume)", () => {
	it("remembers, recalls and forgets the opaque operation id", () => {
		const s = operationStore(memoryStorage());
		expect(s.recall()).toBe("");
		s.remember("llmapply_x");
		expect(s.recall()).toBe("llmapply_x");
		s.forget();
		expect(s.recall()).toBe("");
	});

	it("never throws on a hostile storage (private mode / quota)", () => {
		const throwing = {
			getItem: () => {
				throw new Error("blocked");
			},
			setItem: () => {
				throw new Error("blocked");
			},
			removeItem: () => {
				throw new Error("blocked");
			},
		};
		const s = operationStore(throwing);
		expect(() => s.remember("x")).not.toThrow();
		expect(s.recall()).toBe("");
		expect(() => s.forget()).not.toThrow();
	});
});

describe("createOperationController (the ONE observer)", () => {
	function build(pollSeq, overrides = {}) {
		const clock = fakeClock();
		const updates = [];
		const store = operationStore(memoryStorage());
		let i = 0;
		const poll =
			overrides.poll || vi.fn(async () => pollSeq[Math.min(i++, pollSeq.length - 1)]);
		const controller = createOperationController({
			poll,
			onUpdate: (ui, raw) => updates.push({ ui, raw }),
			now: clock.now,
			setTimer: clock.setTimer,
			clearTimer: clock.clearTimer,
			isVisible: overrides.isVisible || (() => true),
			onVisible: overrides.onVisible || (() => () => {}),
			store,
			random: () => 0, // no jitter, deterministic delays
			config: overrides.config,
		});
		return { clock, updates, store, poll, controller };
	}

	it("follows pending -> applying -> ready and resolves the terminal status", async () => {
		const seq = [statusOf("pending"), statusOf("applying"), statusOf("ready")];
		const { clock, controller, store } = build(seq);
		const p = controller.follow(saveDescriptor);
		await clock.advance(30000);
		const terminal = await p;
		expect(terminal.state).toBe(OP_STATE.READY);
		// The active-operation id is forgotten on terminal (nothing to resume).
		expect(store.recall()).toBe("");
	});

	it("seeds the UI from the save descriptor before the first poll", async () => {
		const { clock, updates, controller } = build([statusOf("ready")]);
		const p = controller.follow(saveDescriptor);
		// Synchronously, the descriptor has been projected once already.
		expect(updates[0].ui.phase).toBe(OP_PHASE.WORKING);
		await clock.advance(5000);
		await p;
	});

	it("remembers the operation id while running (reload resume) and follows a bare id", async () => {
		const { clock, controller, store, poll } = build([
			statusOf("applying"),
			statusOf("ready"),
		]);
		const p = controller.follow(saveDescriptor);
		await clock.advance(0); // first poll
		expect(store.recall()).toBe(OP_IDS.OP_ID);
		await clock.advance(30000);
		await p;
		// A fresh follow by bare id (as a reload would) polls the same operation.
		const p2 = controller.follow(OP_IDS.OP_ID);
		await clock.advance(5000);
		await p2;
		expect(poll).toHaveBeenCalledWith(OP_IDS.OP_ID);
	});

	it("honours a per-operation retry_after cooldown before the next read", async () => {
		// pending (retry_after=10s) then ready. The next poll must wait ~10s, not the
		// ~1.5s base backoff.
		const seq = [statusOf("pending", { retry_after_seconds: 10 }), statusOf("ready")];
		const { clock, controller, poll } = build(seq);
		const p = controller.follow(saveDescriptor);
		await clock.advance(0); // first poll -> pending w/ cooldown
		expect(poll).toHaveBeenCalledTimes(1);
		await clock.advance(5000); // less than the 10s cooldown
		expect(poll).toHaveBeenCalledTimes(1); // still waiting
		await clock.advance(6000); // now past 10s
		expect(poll).toHaveBeenCalledTimes(2);
		await p;
	});

	it("a second follow() supersedes the first: its promise rejects and its polling stops", async () => {
		// Op-id-aware poll: the old op is always pending, so it can only END by being
		// superseded - never by reaching a terminal on its own.
		const poll = vi.fn(async () => statusOf("pending"));
		const { clock, controller } = build([], { poll });
		const p1 = controller.follow(saveDescriptor);
		await clock.advance(0); // first op polled once
		const callsBefore = poll.mock.calls.filter((c) => c[0] === OP_IDS.OP_ID).length;
		expect(callsBefore).toBeGreaterThanOrEqual(1);
		// Supersede with a new operation.
		const p2 = controller.follow(statusOf("pending", { operation_id: "llmapply_NEW" }));
		await expect(p1).rejects.toMatchObject({ aborted: true });
		// The old operation's loop must never poll again after supersession.
		await clock.advance(30000);
		expect(poll.mock.calls.filter((c) => c[0] === OP_IDS.OP_ID).length).toBe(callsBefore);
		// Only the new operation is polled now.
		expect(poll.mock.calls.some((c) => c[0] === "llmapply_NEW")).toBe(true);
		controller.abort();
		await expect(p2).rejects.toMatchObject({ aborted: true });
	});

	it("a poll transport error is not a verdict: it keeps polling to the deadline", async () => {
		let n = 0;
		const poll = vi.fn(async () => {
			n += 1;
			if (n < 3) throw new Error("network");
			return statusOf("ready");
		});
		const { clock, controller } = build([], { poll });
		const p = controller.follow(saveDescriptor);
		await clock.advance(60000);
		const terminal = await p;
		expect(terminal.state).toBe(OP_STATE.READY);
		expect(poll.mock.calls.length).toBeGreaterThanOrEqual(3);
	});

	it("stops at the deadline with a timedOut terminal, never declaring ready", async () => {
		const poll = vi.fn(async () => statusOf("applying")); // never terminal
		const { clock, controller } = build([], { poll, config: { deadlineMs: 10000 } });
		const p = controller.follow(saveDescriptor);
		await clock.advance(60000);
		const terminal = await p;
		expect(terminal.timedOut).toBe(true);
		expect(terminal.state).not.toBe(OP_STATE.READY);
	});

	it("pauses while hidden and resumes on visibility", async () => {
		let visible = false;
		let visCb = null;
		const poll = vi.fn(async () => statusOf("ready"));
		const { clock, controller } = build([], {
			poll,
			isVisible: () => visible,
			onVisible: (cb) => {
				visCb = cb;
				return () => {
					visCb = null;
				};
			},
		});
		const p = controller.follow(saveDescriptor);
		await clock.advance(30000);
		expect(poll).not.toHaveBeenCalled(); // hidden -> no polling
		visible = true;
		if (visCb) visCb();
		await clock.advance(5000);
		expect(poll).toHaveBeenCalled();
		await p;
	});

	it("a terminal reached after supersession cannot resolve the newer run", async () => {
		// Old op resolves ready AFTER a newer follow started; the stale() guard must
		// stop it touching state and it must not resolve p1 with ready.
		let release;
		const gate = new Promise((r) => (release = r));
		const poll = vi.fn(async (id) => {
			if (id === OP_IDS.OP_ID) {
				await gate; // hangs until we release, simulating a slow in-flight read
				return statusOf("ready");
			}
			return statusOf("pending");
		});
		const { clock, controller } = build([], { poll });
		const p1 = controller.follow(saveDescriptor);
		await clock.advance(0); // kicks off the (hanging) first poll
		const p2 = controller.follow(statusOf("pending", { operation_id: "llmapply_NEW" }));
		await expect(p1).rejects.toMatchObject({ aborted: true });
		release(); // the stale in-flight read now resolves ready
		await Promise.resolve();
		await Promise.resolve();
		// p1 already rejected; it must NOT have resolved ready. Clean up p2.
		controller.abort();
		await expect(p2).rejects.toMatchObject({ aborted: true });
	});
});
