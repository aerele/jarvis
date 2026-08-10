import { describe, it, expect, beforeEach, vi } from "vitest";

/**
 * The bug: a reply produced TWO "Reply ready" toasts.
 *
 * A turn's terminal is published more than once (settlement, then the finalize
 * backstop re-publish). ChatView has always deduped that one-shot so its announce
 * and reload fire once per run. This listener, on the same socket, did not, so each
 * doubled terminal signalled twice for one reply.
 *
 * These tests drive the real listener through a fake socket and count toasts, so
 * they fail if the fence is removed rather than merely if the source text changes.
 */

const store = {
	markUnread: vi.fn(),
	applyRemoteNew: vi.fn(),
	currentConvId: null,
	conversations: [{ name: "conv-a", title: "Chat A" }],
	approvalsCount: 0,
};

vi.mock("@/stores/shell", () => ({ useShellStore: () => store }));
vi.mock("@/data/session", () => ({ session: { user: "u@example.com" } }));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));
vi.mock("@/branding", () => ({ agentName: "Jarvis" }));

const { attachGlobalNotifier, useToasts, dismissToast } = await import("./globalNotifier");

/** A socket that hands every emitted payload to the attached listener. */
function fakeSocket() {
	const handlers = [];
	return {
		on: (_evt, fn) => handlers.push(fn),
		off: (_evt, fn) => {
			const i = handlers.indexOf(fn);
			if (i !== -1) handlers.splice(i, 1);
		},
		emit: (payload) => handlers.forEach((fn) => fn(payload)),
	};
}

// Not the conversation any event below names, so signal() takes the toast branch.
// meta.chat falsy means "no conversation is on screen", so signal() takes the toast
// branch, which is the branch the doubling was visible in.
const router = {
	currentRoute: { value: { name: "Approvals", params: {}, meta: {} } },
	push: vi.fn(),
};

function terminal(over = {}) {
	return {
		kind: "run:end",
		conversation_id: "conv-a",
		run_id: "run-1",
		pump_epoch: 3,
		event_seq: 12,
		preview: "All done.",
		...over,
	};
}

let socket, detach;

// jsdom exposes document.hidden as a getter with no setter, so assignment throws.
function setHidden(v) {
	Object.defineProperty(document, "hidden", { configurable: true, get: () => v });
}

beforeEach(() => {
	for (const t of [...useToasts().value]) dismissToast(t.id);
	vi.clearAllMocks();
	setHidden(false);
	socket = fakeSocket();
	detach = attachGlobalNotifier({ socket, router });
});

describe("a re-published terminal signals once", () => {
	it("toasts ONCE for the same terminal delivered twice", () => {
		// THE bug: settlement publishes it, then the finalize backstop re-publishes
		// the identical frame.
		socket.emit(terminal());
		socket.emit(terminal());
		expect(useToasts().value).toHaveLength(1);
	});

	it("toasts once even when the repeat arrives at a LOWER epoch", () => {
		// A superseded writer's late terminal, which must never re-signal.
		socket.emit(terminal({ pump_epoch: 3 }));
		socket.emit(terminal({ pump_epoch: 2 }));
		expect(useToasts().value).toHaveLength(1);
	});

	it("dedupes a re-published run:error the same way", () => {
		socket.emit(terminal({ kind: "run:error", error: "It broke." }));
		socket.emit(terminal({ kind: "run:error", error: "It broke." }));
		expect(useToasts().value).toHaveLength(1);
	});

	it("still signals a DIFFERENT run", () => {
		// The fence must dedupe repeats, not swallow the next reply.
		socket.emit(terminal({ run_id: "run-1" }));
		socket.emit(terminal({ run_id: "run-2" }));
		expect(useToasts().value).toHaveLength(2);
	});

	it("still signals a genuinely newer epoch for the same run", () => {
		// A recovered turn re-runs at a higher epoch and IS a new outcome.
		socket.emit(terminal({ pump_epoch: 3 }));
		socket.emit(terminal({ pump_epoch: 4, event_seq: 20 }));
		expect(useToasts().value).toHaveLength(2);
	});

	it("leaves an epoch-less legacy terminal alone", () => {
		// No pump_epoch means the fence has nothing to reason about, so the frame is
		// applied unchanged. Documented behaviour, matching ChatView.
		socket.emit(terminal({ pump_epoch: undefined, event_seq: undefined }));
		expect(useToasts().value).toHaveLength(1);
	});
});

describe("the fence does not touch the other signals", () => {
	it("still toasts a parked confirmation, which is a separate event", () => {
		// action:pending and run:end are different things and each deserves its own
		// signal. Two DIFFERENT toasts for one turn is intended, unlike two identical ones.
		socket.emit({
			kind: "action:pending",
			conversation: "conv-a",
			tool: "jarvis__create_doc",
		});
		socket.emit(terminal());
		expect(useToasts().value).toHaveLength(2);
	});

	it("stops signalling once detached", () => {
		detach();
		socket.emit(terminal({ run_id: "run-9" }));
		expect(useToasts().value).toHaveLength(0);
	});
});

describe("a hidden tab takes the browser-notification branch, not the toast", () => {
	it("does not stack toasts while hidden", () => {
		setHidden(true);
		socket.emit(terminal());
		socket.emit(terminal());
		expect(useToasts().value).toHaveLength(0);
	});
});
