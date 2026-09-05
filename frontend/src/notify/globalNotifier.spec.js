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

// Recent Node versions expose an experimental localStorage getter that is
// undefined without --localstorage-file, which can shadow jsdom's storage.
// Use an explicit browser-shaped in-memory store for these routing tests.
const storage = new Map();
const localStorageStub = {
	getItem: (key) => (storage.has(key) ? storage.get(key) : null),
	setItem: (key, value) => storage.set(key, String(value)),
	removeItem: (key) => storage.delete(key),
	clear: () => storage.clear(),
};
vi.stubGlobal("localStorage", localStorageStub);
window.focus = vi.fn();

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
	store.currentConvId = null;
	store.approvalsCount = 0;
	localStorage.clear();
	router.currentRoute.value = { name: "Approvals", params: {}, meta: {} };
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

describe("dashboard-origin attention stays in Dashboard Builder", () => {
	it("does not toast a parked action already visible in the builder pane", () => {
		localStorage.setItem("jarvis-dash-conv-u@example.com", "conv-a");
		router.currentRoute.value = { name: "DashboardsPage", hash: "", params: {}, meta: {} };
		socket.emit({
			kind: "action:pending",
			conversation: "conv-a",
			origin_page: "dashboards",
			tool: "jarvis__save_dashboard",
		});
		expect(useToasts().value).toHaveLength(0);
	});

	it("routes an off-screen dashboard confirmation back to its builder conversation", () => {
		socket.emit({
			kind: "action:pending",
			conversation: "dash-conv",
			origin_page: "dashboards",
			tool: "jarvis__save_dashboard",
		});
		expect(useToasts().value).toHaveLength(1);
		useToasts().value[0].onClick();
		expect(router.push).toHaveBeenCalledWith({
			name: "DashboardsPage",
			query: { conversation: "dash-conv" },
		});
	});

	it("suppresses dashboard approval redirects while its AskCard is visible", () => {
		localStorage.setItem("jarvis-dash-conv-u@example.com", "conv-a");
		router.currentRoute.value = { name: "DashboardsPage", hash: "", params: {}, meta: {} };
		socket.emit({
			kind: "approval:new",
			conversation_id: "conv-a",
			origin_page: "dashboards",
			question: "Which metric?",
		});
		expect(useToasts().value).toHaveLength(0);
		expect(store.approvalsCount).toBe(1);
	});

	it("opens an off-screen dashboard question in the builder, not Approval Board", () => {
		socket.emit({
			kind: "approval:new",
			conversation_id: "dash-conv",
			origin_page: "dashboards",
			question: "Which metric?",
		});
		expect(useToasts().value).toHaveLength(1);
		useToasts().value[0].onClick();
		expect(router.push).toHaveBeenCalledWith({
			name: "DashboardsPage",
			query: { conversation: "dash-conv" },
		});
	});

	it("does not create a main-chat unread dot for a dashboard terminal", () => {
		// The user has already switched the pane to another dashboard thread, so
		// only the terminal's durable origin can classify this background result.
		localStorage.setItem("jarvis-dash-conv-u@example.com", "another-dashboard");
		socket.emit(terminal({ origin_page: "dashboards" }));
		expect(store.markUnread).not.toHaveBeenCalled();
		expect(useToasts().value).toHaveLength(1);
		useToasts().value[0].onClick();
		expect(router.push).toHaveBeenCalledWith({
			name: "DashboardsPage",
			query: { conversation: "conv-a" },
		});
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

// Slice B ("auto-tell import completion"): a background Data Import posts its
// "✓ ..." message into the conversation and publishes
// { kind: "import:finished", conversation_id, message_id } to the owner. This
// listener owns only the OFF-SCREEN half (the sidebar unread dot) — ChatView's
// own onEvent (see importFinishedRender.spec.js) re-reads the transcript when
// that conversation is the one on screen. The plan is explicit that this wave
// ships NO push toast/browser notification for this event, so these tests pin
// markUnread as a DIRECT call, bypassing signal()/browserNotify entirely.
describe("import:finished marks the sidebar unread, never a toast", () => {
	it("calls store.markUnread directly for an off-screen conversation", () => {
		socket.emit({ kind: "import:finished", conversation_id: "conv-a", message_id: "msg-1" });
		expect(store.markUnread).toHaveBeenCalledWith("conv-a");
		expect(useToasts().value).toHaveLength(0);
	});

	it("still only marks unread — no toast — even while the tab is hidden", () => {
		// The hidden branch is where run:end/run:error escalate to a browser
		// notification (signal()'s first branch). import:finished must never
		// reach signal() at all, so nothing renders here either.
		setHidden(true);
		socket.emit({ kind: "import:finished", conversation_id: "conv-a", message_id: "msg-1" });
		expect(store.markUnread).toHaveBeenCalledWith("conv-a");
		expect(useToasts().value).toHaveLength(0);
	});

	it("does nothing for a conversation-less event, rather than marking ambiently", () => {
		socket.emit({ kind: "import:finished", message_id: "msg-1" });
		expect(store.markUnread).not.toHaveBeenCalled();
	});

	it("leaves the ON-screen conversation alone — that's ChatView's job, not this listener's", () => {
		const savedRoute = router.currentRoute.value;
		store.currentConvId = "conv-a";
		router.currentRoute.value = {
			name: "Chat",
			params: { id: "conv-a" },
			meta: { chat: true },
		};
		try {
			socket.emit({
				kind: "import:finished",
				conversation_id: "conv-a",
				message_id: "msg-1",
			});
			expect(store.markUnread).not.toHaveBeenCalled();
		} finally {
			router.currentRoute.value = savedRoute;
			store.currentConvId = null;
		}
	});
});
