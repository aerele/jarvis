// Reset Workspace / Disconnect this bench (Settings > General): the depth
// ladder composes the right flags, the two lockout paths (L4 and the separate
// Disconnect action) show their cost and their way back before they can be
// submitted, and the terminal `disconnected` state is never mistaken for a
// plain unreachable-admin blip or a normal "ready, reloading" reset.
//
// Mounted via @vue/test-utils and driven through the exposed <script setup>
// bindings (w.vm.*) rather than DOM clicks - the same convention
// LlmPoolEditor.spec.js in this directory uses for its disconnect coverage.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// GeneralPane.vue -> stores/shell.js reads window.matchMedia at MODULE
// EVALUATION time (its narrow-sidebar breakpoint watcher, not inside a
// function), so this has to run before the `import GeneralPane` below even
// begins to resolve, not just before the first test. vi.hoisted is what makes
// that ordering guarantee - a plain top-of-file statement would still run
// after import hoisting. (OnboardingView.spec.js's identical stub lives in
// beforeEach instead, because the module it mounts never reads matchMedia
// this early.)
vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent() {},
	});
});

const api = vi.hoisted(() => ({
	getUsage: vi.fn(),
	getMySettings: vi.fn(),
	getLlmConnectionStatus: vi.fn(),
	requestWorkspaceReset: vi.fn(),
	workspaceResetState: vi.fn(),
	disconnectBench: vi.fn(),
	benchConnectionState: vi.fn(),
}));
vi.mock("@/api", () => api);

// frappe-ui components are rendered but never asserted on directly here
// (every check goes through exposed script-setup state instead), so trivial
// templates are enough - matches OnboardingView.spec.js's stub shape.
vi.mock("frappe-ui", () => ({
	Badge: { name: "Badge", template: "<span><slot />{{ label }}</span>", props: ["label"] },
	Button: { name: "Button", template: "<button @click=\"$emit('click')\"><slot /></button>" },
	Switch: { name: "Switch", template: "<input type=\"checkbox\" />" },
	ErrorMessage: { name: "ErrorMessage", template: "<div><slot /></div>" },
	toast: { error: vi.fn(), success: vi.fn() },
}));

const answer = vi.hoisted(() => ({ confirm: true }));
const confirmCalls = vi.hoisted(() => []);
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({
		confirm: async (opts) => {
			confirmCalls.push(opts);
			return answer.confirm;
		},
	}),
}));

import { toast } from "frappe-ui";
import GeneralPane from "./GeneralPane.vue";

async function mountPane() {
	const w = mount(GeneralPane);
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	confirmCalls.length = 0;
	answer.confirm = true;
	window.is_jarvis_admin = true;
	window.is_system_manager = false;
	// {} (rather than null) is a truthy "no usage yet" that still lacks
	// month_label - out of scope for this suite, so avoid the unrelated KvRow
	// prop warning it triggers.
	api.getUsage.mockResolvedValue(null);
	api.getMySettings.mockResolvedValue({ ok: true, data: {} });
	api.getLlmConnectionStatus.mockResolvedValue({});
	api.requestWorkspaceReset.mockResolvedValue({});
	api.workspaceResetState.mockResolvedValue({});
	api.disconnectBench.mockResolvedValue({
		disconnected: true,
		already_disconnected: false,
		cleared: [],
		needs_company: false,
	});
	// Default: not disconnected, so resumeResetIfInFlight / pollReset fall
	// through to whatever workspaceResetState says, unchanged from before this
	// mock existed - tests that care about the disconnected branch override it.
	api.benchConnectionState.mockResolvedValue({ disconnected: false, needs_company: false });
	// jsdom's window.location.assign is non-configurable, so it cannot be
	// vi.spyOn'd directly - replace the whole object instead (goReconnect and
	// the ready-reload path both call this).
	Object.defineProperty(window, "location", {
		configurable: true,
		value: { assign: vi.fn() },
	});
});

afterEach(() => {
	vi.useRealTimers();
});

describe("the depth ladder composes flags cumulatively", () => {
	// Each depth adds to the one above it (plan: "four depths, each adding to
	// the one above") - a radio ladder instead of independent checkboxes makes
	// "L3 without L2" unrepresentable rather than merely undocumented.
	it.each([
		[1, { wipeData: false, revokeLlm: false, disconnectAfter: false }],
		[2, { wipeData: true, revokeLlm: false, disconnectAfter: false }],
		[3, { wipeData: true, revokeLlm: true, disconnectAfter: false }],
		[4, { wipeData: true, revokeLlm: true, disconnectAfter: true }],
	])("depth %i sends %o", async (depth, expected) => {
		const w = await mountPane();
		w.vm.resetDepth = depth;
		await w.vm.doReset();
		expect(api.requestWorkspaceReset).toHaveBeenCalledWith("", expected);
		w.vm.stopPoll();
	});
});

describe("L4 is a lockout path", () => {
	it("shows the cost and the emailed-code recovery before submitting, not after", async () => {
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_DISCONNECT;
		await w.vm.doReset();
		const shown = confirmCalls[confirmCalls.length - 1];
		expect(shown.message).toMatch(/disconnects from your account/i);
		expect(shown.message).toMatch(/one-time code emailed/i);
		expect(shown.message).toMatch(/company name/i);
		expect(shown.danger).toBe(true);
		w.vm.stopPoll();
	});

	it("does not claim a disconnect for the shallower depths", async () => {
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_REVOKE_LLM;
		await w.vm.doReset();
		const shown = confirmCalls[confirmCalls.length - 1];
		expect(shown.message).not.toMatch(/disconnects from your account/i);
		w.vm.stopPoll();
	});

	it("refuses up front verbatim and starts nothing when reconnect would not work", async () => {
		api.requestWorkspaceReset.mockRejectedValue({
			messages: ["No reconnectable account was found for a@b.com. Contact support instead."],
		});
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_DISCONNECT;
		await w.vm.doReset();
		expect(toast.error).toHaveBeenCalledWith(
			"No reconnectable account was found for a@b.com. Contact support instead."
		);
		expect(w.vm.resetting).toBe(false);
		expect(w.vm.benchDisconnected).toBe(false);
	});

	// T33 / round-5 MINOR 12. The refusal case above was the only half exercised;
	// the half Amendment 4 singles out — the call that never answered — had no test.
	it("starts polling when the initiate call dies without answering", async () => {
		// gunicorn SIGKILL / dropped connection: no status, no server messages.
		api.requestWorkspaceReset.mockRejectedValue(new Error("Network Error"));
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_REBUILD;
		await w.vm.doReset();
		// Admin rebuilds SYNCHRONOUSLY inside the request, so the reset may well be
		// running, and the bench keeps its claim for exactly that reason. Without
		// this the customer sits on a bare error toast watching nothing happen.
		expect(w.vm.resetting).toBe(true);
		w.vm.stopPoll();
	});

	it("does NOT start polling when the server answered with a refusal", async () => {
		// A 4xx is a decision: nothing was started, so a spinner would be a lie.
		api.requestWorkspaceReset.mockRejectedValue({
			status: 417,
			messages: ["A workspace reset is already running at a different depth."],
		});
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_REBUILD;
		await w.vm.doReset();
		expect(w.vm.resetting).toBe(false);
	});

	it("treats a JSON-bodied 502 as unreachable despite the synthesised message", async () => {
		// round-5 MINOR 11: frappe-ui's call.js sets messages:['Internal Server
		// Error'] for any parseable JSON error body, so keying on `messages` first
		// misread this as a refusal. Status is the discriminator.
		api.requestWorkspaceReset.mockRejectedValue({
			status: 502,
			messages: ["Internal Server Error"],
		});
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_REBUILD;
		await w.vm.doReset();
		expect(w.vm.resetting).toBe(true);
		w.vm.stopPoll();
	});

	it("does nothing without confirmation", async () => {
		answer.confirm = false;
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_DISCONNECT;
		await w.vm.doReset();
		expect(api.requestWorkspaceReset).not.toHaveBeenCalled();
	});

	it("closeReset returns the ladder to the harmless default", async () => {
		const w = await mountPane();
		w.vm.resetDepth = w.vm.DEPTH_DISCONNECT;
		w.vm.closeReset();
		expect(w.vm.resetDepth).toBe(w.vm.DEPTH_REBUILD);
	});
});

describe("the poll tells a completed L4 disconnect apart from a normal ready reload", () => {
	it("a disconnected+ready poll enters the terminal state and never reloads the page", async () => {
		const w = await mountPane();
		api.workspaceResetState.mockResolvedValue({ ready: true, disconnected: true });
		w.vm.resetting = true;
		w.vm.pollStarted = Date.now();
		await w.vm.pollReset();
		expect(w.vm.benchDisconnected).toBe(true);
		expect(w.vm.resetting).toBe(false);
		await new Promise((r) => setTimeout(r, 900));
		expect(window.location.assign).not.toHaveBeenCalled();
	});

	// T23 / round-4 MAJOR 9. This used to assert ONLY the base sentence, on the
	// reasoning that needs_company was "always definitive". It was definitive in
	// the sense that it could never be anything but false: bench_connection_state
	// hardcoded it, and pollReset resolves the value from that endpoint. So the
	// test passed because the mock echoed a constant the real endpoint could not
	// vary from - a green test certifying a property the system did not have.
	//
	// It varies now (persisted onto reconnect_needs_company at the moment of the
	// clear), so both values are pinned. A customer whose registered address owns
	// several eligible accounts MUST be told to have the company name ready, or
	// they are sent to a reconnect that cannot complete with what they were given.
	it("names the company requirement when the address owns several accounts", async () => {
		const w = await mountPane();
		api.benchConnectionState.mockResolvedValue({ disconnected: true, needs_company: true });
		api.workspaceResetState.mockResolvedValue({ ready: true, disconnected: true });
		w.vm.resetting = true;
		w.vm.pollStarted = Date.now();
		await w.vm.pollReset();
		expect(w.vm.benchNeedsCompany).toBe(true);
		expect(w.vm.disconnectRecoveryText).toContain("company name");
	});

	it("omits the company caveat when the address owns exactly one account", async () => {
		const w = await mountPane();
		api.benchConnectionState.mockResolvedValue({ disconnected: true, needs_company: false });
		api.workspaceResetState.mockResolvedValue({ ready: true, disconnected: true });
		w.vm.resetting = true;
		w.vm.pollStarted = Date.now();
		await w.vm.pollReset();
		expect(w.vm.benchNeedsCompany).toBe(false);
		expect(w.vm.disconnectRecoveryText).toBe(
			"Reconnect with the one-time code emailed to this workspace's registered address."
		);
	});

	// T24 / round-4 MAJOR 2.
	it("tells the customer when an L4 was downgraded to an L3, and does not reload", async () => {
		const w = await mountPane();
		api.workspaceResetState.mockResolvedValue({
			ready: true,
			disconnected: false,
			disconnect_blocked: "Subscription is Cancelled; reconnect is not available.",
		});
		w.vm.resetting = true;
		w.vm.pollStarted = Date.now();
		await w.vm.pollReset();
		expect(w.vm.resetting).toBe(false);
		expect(toast.error).toHaveBeenCalledWith(
			expect.stringContaining("could not be disconnected")
		);
		// A reload would drop the only message explaining the downgrade, and there
		// is nothing to reload for: the connection is unchanged.
		await new Promise((r) => setTimeout(r, 900));
		expect(window.location.assign).not.toHaveBeenCalled();
	});

	it("a plain ready poll (no disconnect) still reloads, unchanged from before", async () => {
		vi.useFakeTimers();
		const w = await mountPane();
		api.workspaceResetState.mockResolvedValue({ ready: true });
		w.vm.resetting = true;
		w.vm.pollStarted = Date.now();
		await w.vm.pollReset();
		expect(w.vm.benchDisconnected).toBe(false);
		await vi.advanceTimersByTimeAsync(1000);
		expect(window.location.assign).toHaveBeenCalledWith("/jarvis/");
	});
});

describe("resuming an in-flight reset on mount", () => {
	it("catches an L4 disconnect completed by the resume call itself", async () => {
		// _workspace_reset_poll is not read-only: persisting the fresh connection
		// and clearing admin creds are side effects of calling it. A tab closed in
		// the last second of an L4 rebuild means the NEXT mount's own resume poll
		// is the call that finishes the disconnect - it must not go unnoticed.
		api.workspaceResetState.mockResolvedValue({ ready: true, disconnected: true });
		const w = await mountPane();
		expect(w.vm.benchDisconnected).toBe(true);
		expect(w.vm.resetting).toBe(false);
	});

	it("resumes an ordinary in-flight reset as before", async () => {
		api.workspaceResetState.mockResolvedValue({ ready: false, resetting: true });
		const w = await mountPane();
		expect(w.vm.resetting).toBe(true);
		expect(w.vm.benchDisconnected).toBe(false);
		w.vm.stopPoll();
	});

	it("checks benchConnectionState before workspaceResetState, and trusts a disconnected answer even though workspaceResetState would reject", async () => {
		// The real defect this ordering fixes: a bench with no admin credentials
		// left (already disconnected) cannot call the AUTHENTICATED
		// workspace_reset_state endpoint - it would 401. The old order called
		// workspaceResetState() first, so that rejection was swallowed by the
		// "nothing to resume" catch and the disconnected bench reloaded into the
		// generic onboarding poster with no trace of how to get back.
		// benchConnectionState() reads local settings only and needs no
		// credentials, so checking it FIRST and trusting a disconnected answer
		// must short-circuit before the credential-hungry call is ever reached.
		const callOrder = [];
		api.benchConnectionState.mockImplementation(async () => {
			callOrder.push("benchConnectionState");
			return { disconnected: true, needs_company: false };
		});
		api.workspaceResetState.mockImplementation(async () => {
			callOrder.push("workspaceResetState");
			return Promise.reject({ messages: ["not authenticated"] });
		});
		const w = await mountPane();
		expect(callOrder).toEqual(["benchConnectionState"]);
		expect(api.workspaceResetState).not.toHaveBeenCalled();
		expect(w.vm.benchDisconnected).toBe(true);
		expect(w.vm.resetting).toBe(false);
	});
});

describe("Disconnect this bench (separate terminal action)", () => {
	it("shows the cost and the recovery code before submitting", async () => {
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		const shown = confirmCalls[confirmCalls.length - 1];
		expect(shown.message).toMatch(/one-time code emailed/i);
		expect(shown.message).toMatch(/chat stops working immediately/i);
		expect(shown.danger).toBe(true);
	});

	it("does nothing without confirmation", async () => {
		answer.confirm = false;
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		expect(api.disconnectBench).not.toHaveBeenCalled();
	});

	it("tells the customer they will need their company name when needs_company is true", async () => {
		api.disconnectBench.mockResolvedValue({
			disconnected: true,
			already_disconnected: false,
			cleared: ["agent_url"],
			needs_company: true,
		});
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		expect(w.vm.benchDisconnected).toBe(true);
		expect(w.vm.disconnectRecoveryText).toMatch(/you'll also need to give the company name/i);
		expect(toast.success).toHaveBeenCalled();
	});

	it("omits the company caveat when needs_company is definitively false", async () => {
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		expect(w.vm.disconnectRecoveryText).not.toMatch(/company/i);
	});

	it("treats a double disconnect as idempotent, not an error", async () => {
		api.disconnectBench.mockResolvedValue({
			disconnected: true,
			already_disconnected: true,
			cleared: [],
			needs_company: false,
		});
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		expect(w.vm.benchDisconnected).toBe(true);
		expect(toast.error).not.toHaveBeenCalled();
		expect(toast.success).toHaveBeenCalledWith("This bench was already disconnected.");
	});

	it("refuses verbatim and changes nothing when reconnect would not work", async () => {
		api.disconnectBench.mockRejectedValue({
			messages: ["No live workspace was found, so disconnecting now would lock you out."],
		});
		const w = await mountPane();
		await w.vm.doDisconnectBench();
		expect(toast.error).toHaveBeenCalledWith(
			"No live workspace was found, so disconnecting now would lock you out."
		);
		expect(w.vm.benchDisconnected).toBe(false);
	});

	it("is blocked while a reset is in flight, so it cannot race the poll's own clear", async () => {
		const w = await mountPane();
		w.vm.resetting = true;
		await w.vm.doDisconnectBench();
		expect(api.disconnectBench).not.toHaveBeenCalled();
	});
});
