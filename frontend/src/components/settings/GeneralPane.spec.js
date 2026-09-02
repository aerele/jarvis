import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Settings Status badge, from a MEMBER's seat (jarvis#711).
 *
 * The badge used to be a hardcoded green "Connected" for anyone who was not an
 * admin, whatever the workspace was doing. These tests pin the replacement, and
 * the one that matters most is the first: a member with no verdict yet - which
 * is EVERY member for the whole first paint, before any fetch resolves - must
 * render the neutral state, not green. A future refactor that reintroduces a
 * green default has to fail here.
 */

// @/stores/shell reads matchMedia at MODULE level, which runs on import, before
// any beforeEach could install a stub. Same hoisted shim the other component
// specs use.
vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

// frappe-ui's ESM entry does not resolve under vitest. The stubs keep the two
// props these tests read (Badge's label + theme, Button's label) observable in
// the DOM instead of only in component internals - the assertions below are
// about what a member SEES.
vi.mock("frappe-ui", () => {
	const passthrough = (name, tag) => ({
		name,
		props: ["label", "theme", "variant", "iconLeft", "loading", "disabled", "modelValue"],
		template: `<${tag} class="stub-${name.toLowerCase()}" :data-theme="theme" :data-label="label"><slot /></${tag}>`,
	});
	return {
		Badge: passthrough("Badge", "span"),
		Button: passthrough("Button", "button"),
		Switch: passthrough("Switch", "span"),
		ErrorMessage: passthrough("ErrorMessage", "span"),
		toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
		call: vi.fn(),
	};
});

const openSettings = vi.fn();
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({
		chatContext: null,
		openSettings,
		activityDetail: false,
		notifyEnabled: false,
		notifySupported: false,
		settingsActions: {},
		setActivityDetail: vi.fn(),
		toggleNotify: vi.fn(),
		syncSettingsFromServer: vi.fn(),
	}),
}));

// Shared so the Reset-onboarding tests can drive the danger confirm's answer.
const { confirmMock } = vi.hoisted(() => ({ confirmMock: vi.fn() }));
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm: confirmMock }),
}));

// Every network call the pane makes on mount. getUsage / getMySettings /
// workspaceResetState are best-effort in the component and irrelevant here, so
// they resolve empty; the two connection calls are what each test drives.
vi.mock("@/api", () => ({
	getUsage: vi.fn(() => Promise.resolve(null)),
	getMySettings: vi.fn(() => Promise.resolve({ ok: false })),
	workspaceResetState: vi.fn(() => Promise.resolve({})),
	requestWorkspaceReset: vi.fn(() => Promise.resolve({})),
	resetOnboarding: vi.fn(() => Promise.resolve({ ok: true })),
	clearAllHistory: vi.fn(() => Promise.resolve({})),
	getLlmConnectionStatus: vi.fn(() => new Promise(() => {})),
	getLlmConnectionHealth: vi.fn(() => new Promise(() => {})),
}));

import * as api from "@/api";
import GeneralPane from "./GeneralPane.vue";

/** The Status row's badge: its label and its theme, as rendered. */
function badge(w) {
	const el = w.find(".stub-badge");
	if (!el.exists()) return { label: "", theme: "" };
	return { label: el.attributes("data-label") || "", theme: el.attributes("data-theme") || "" };
}

/** Labels of every Button the pane rendered. */
function buttonLabels(w) {
	return w.findAll(".stub-button").map((b) => b.attributes("data-label") || "");
}

async function mountAs({ admin, jarvisAdmin = false }) {
	window.is_system_manager = admin;
	window.is_jarvis_admin = jarvisAdmin;
	const w = mount(GeneralPane);
	await flushPromises();
	return w;
}

/** A member health fetch that never settles: the real first-paint condition. */
const PENDING = () => new Promise(() => {});

beforeEach(() => {
	vi.clearAllMocks();
	api.getUsage.mockImplementation(() => Promise.resolve(null));
	api.getMySettings.mockImplementation(() => Promise.resolve({ ok: false }));
	api.workspaceResetState.mockImplementation(() => Promise.resolve({}));
	api.getLlmConnectionStatus.mockImplementation(PENDING);
	api.getLlmConnectionHealth.mockImplementation(PENDING);
});

afterEach(() => {
	delete window.is_system_manager;
	delete window.is_jarvis_admin;
});

describe("GeneralPane Status badge, member seat", () => {
	// THE regression test for #711. Not "a member with a bad verdict sees red" -
	// this is the degenerate case production actually starts from: the verdict is
	// simply not here yet. The old code answered it with green.
	it("is neutral, never green, before any verdict has arrived", async () => {
		api.getLlmConnectionHealth.mockImplementation(PENDING);
		const w = await mountAs({ admin: false });
		const b = badge(w);
		expect(b.theme).not.toBe("green");
		expect(b.label).not.toBe("Connected");
		expect(b.theme).toBe("gray");
		expect(b.label).toBe("-");
	});

	// The other half of "fail closed": a state the SPA has no mapping for must
	// not fall through to the healthy branch either. A server that grows a fifth
	// verdict, or a truncated payload, both land here.
	it.each([
		["a payload with no state at all", {}],
		["a null payload", null],
		["an unrecognised state", { state: "quiescent" }],
		["an empty state", { state: "" }],
	])("is neutral, never green, for %s", async (_name, payload) => {
		api.getLlmConnectionHealth.mockImplementation(() => Promise.resolve(payload));
		const w = await mountAs({ admin: false });
		const b = badge(w);
		expect(b.theme).not.toBe("green");
		expect(b.label).not.toBe("Connected");
		expect(b.theme).toBe("gray");
	});

	it("shows red Needs attention when the workspace is unwell", async () => {
		api.getLlmConnectionHealth.mockImplementation(() =>
			Promise.resolve({ state: "attention" })
		);
		const w = await mountAs({ admin: false });
		expect(badge(w)).toEqual({ label: "Needs attention", theme: "red" });
	});

	it("shows red Not connected when the assistant is not serving", async () => {
		api.getLlmConnectionHealth.mockImplementation(() => Promise.resolve({ state: "down" }));
		const w = await mountAs({ admin: false });
		expect(badge(w)).toEqual({ label: "Not connected", theme: "red" });
	});

	it("shows blue Applying changes while a save is in flight", async () => {
		api.getLlmConnectionHealth.mockImplementation(() =>
			Promise.resolve({ state: "applying" })
		);
		const w = await mountAs({ admin: false });
		expect(badge(w)).toEqual({ label: "Applying changes", theme: "blue" });
	});

	it("shows green Connected only when the server says ok", async () => {
		api.getLlmConnectionHealth.mockImplementation(() => Promise.resolve({ state: "ok" }));
		const w = await mountAs({ admin: false });
		expect(badge(w)).toEqual({ label: "Connected", theme: "green" });
	});

	// A failed fetch is not a healthy workspace either, and a member has to be
	// able to retry it - the same affordance an admin already had.
	it("falls back to neutral and offers a retry when the fetch fails", async () => {
		api.getLlmConnectionHealth.mockImplementation(() => Promise.reject(new Error("403")));
		const w = await mountAs({ admin: false });
		expect(badge(w).theme).not.toBe("green");
		expect(w.text()).toContain("Connection status is unavailable right now.");
		expect(buttonLabels(w)).toContain("Retry");
	});

	// The member endpoint is the smallest thing that renders the badge; the admin
	// one returns pool shape, model names and profile ids, so a member must never
	// reach it. This also pins that the pane does not simply try both.
	it("never calls the admin connection endpoint", async () => {
		api.getLlmConnectionHealth.mockImplementation(() => Promise.resolve({ state: "ok" }));
		await mountAs({ admin: false });
		expect(api.getLlmConnectionStatus).not.toHaveBeenCalled();
		expect(api.getLlmConnectionHealth).toHaveBeenCalledTimes(1);
	});

	// A member has no attention_reason (the server does not send one) and cannot
	// open the AI models pane: a gated section falls back to General silently, so
	// the button would look like a dead end. The hint has to stand alone.
	it("gives an attention hint with no AI models button and no cause", async () => {
		api.getLlmConnectionHealth.mockImplementation(() =>
			Promise.resolve({ state: "attention" })
		);
		const w = await mountAs({ admin: false });
		expect(buttonLabels(w)).not.toContain("Open AI models");
		const text = w.text();
		expect(text).toContain("Your workspace needs attention.");
		expect(text).not.toContain("Open AI models");
		expect(text).not.toContain("subscription");
	});

	// The disclosure decision, enforced at the surface: whatever is behind
	// "attention", a member sees one sentence. The server sends no reason, so a
	// pane that started rendering one would be inventing it.
	it("renders the same attention copy whatever the server reason would be", async () => {
		const texts = [];
		for (const payload of [
			{ state: "attention" },
			{ state: "attention", attention_reason: "subscription_unverified" },
			{ state: "attention", attention_reason: "turn_error" },
			{ state: "attention", attention_reason: "sync_failed" },
		]) {
			api.getLlmConnectionHealth.mockImplementation(() => Promise.resolve(payload));
			const w = await mountAs({ admin: false });
			texts.push(w.text());
		}
		expect(new Set(texts).size).toBe(1);
	});
});

describe("GeneralPane Status badge, admin seat", () => {
	it("still uses the admin endpoint and its full payload", async () => {
		api.getLlmConnectionStatus.mockImplementation(() =>
			Promise.resolve({
				pool_mode: true,
				model_count: 3,
				routing_mode: "failover",
				sync_status: "ok (pool_update via admin)",
				proxy_active: true,
				disconnected: false,
				health: "ok",
				attention_reason: "",
			})
		);
		const w = await mountAs({ admin: true });
		expect(api.getLlmConnectionHealth).not.toHaveBeenCalled();
		expect(badge(w)).toEqual({ label: "Connected", theme: "green" });
		expect(w.text()).toContain("3 models");
	});

	// An admin keeps the cause-specific hint and the button that acts on it -
	// #710/#714's work is untouched by the member split.
	it("keeps the per-cause attention hint", async () => {
		api.getLlmConnectionStatus.mockImplementation(() =>
			Promise.resolve({ health: "attention", attention_reason: "sync_failed" })
		);
		const w = await mountAs({ admin: true });
		expect(badge(w)).toEqual({ label: "Needs attention", theme: "red" });
		expect(w.text()).toContain("did not reach your agent");
		expect(buttonLabels(w)).toContain("Open AI models");
	});

	// The admin's own not-yet-known state, which the member state now mirrors.
	it("is neutral before its verdict arrives", async () => {
		api.getLlmConnectionStatus.mockImplementation(PENDING);
		const w = await mountAs({ admin: true });
		expect(badge(w).theme).not.toBe("green");
		expect(badge(w)).toEqual({ label: "-", theme: "gray" });
	});

	// disconnected stays an admin-only distinction: a member gets "down" for the
	// same workspace, so credential presence is never disclosed to them.
	it("keeps Disconnected as its own orange state", async () => {
		api.getLlmConnectionStatus.mockImplementation(() =>
			Promise.resolve({ disconnected: true, health: "down" })
		);
		const w = await mountAs({ admin: true });
		expect(badge(w)).toEqual({ label: "Disconnected", theme: "orange" });
	});
});

describe("GeneralPane Reset onboarding button", () => {
	function resetBtn(w) {
		return w
			.findAll(".stub-button")
			.find((b) => b.attributes("data-label") === "Reset to onboarding");
	}

	beforeEach(() => {
		confirmMock.mockReset();
		api.resetOnboarding.mockReset();
		api.resetOnboarding.mockImplementation(() => Promise.resolve({ ok: true }));
	});

	it("is offered to an admin and hidden from a member", async () => {
		const wAdmin = await mountAs({ admin: true });
		expect(buttonLabels(wAdmin)).toContain("Reset to onboarding");
		const wMember = await mountAs({ admin: false });
		expect(buttonLabels(wMember)).not.toContain("Reset to onboarding");
	});

	// The backend (dev.reset_onboarding) is only_for("System Manager"), stricter
	// than the sibling Reset workspace. A Jarvis-Admin-only seat (is_jarvis_admin
	// but not is_system_manager) must NOT see this button, or it 403s as a dead
	// control. It still sees the rest of the danger zone (gated on isSM).
	it("is hidden from a Jarvis-Admin-only seat that the backend would reject", async () => {
		const w = await mountAs({ admin: false, jarvisAdmin: true });
		expect(buttonLabels(w)).not.toContain("Reset to onboarding");
		// The danger zone itself renders for this seat — only this stricter button is gone.
		expect(buttonLabels(w)).toContain("Reset workspace");
	});

	it("does nothing when the danger confirm is dismissed", async () => {
		confirmMock.mockResolvedValue(false);
		const w = await mountAs({ admin: true });
		await resetBtn(w).trigger("click");
		await flushPromises();
		expect(confirmMock).toHaveBeenCalledTimes(1);
		expect(api.resetOnboarding).not.toHaveBeenCalled();
	});

	it("full-wipes only after the confirm is accepted", async () => {
		confirmMock.mockResolvedValue(true);
		const w = await mountAs({ admin: true });
		await resetBtn(w).trigger("click");
		await flushPromises();
		// The danger confirm must have been the gate, and the call is the
		// CLI-matching full reset (wipe_data=true), not connection-only.
		expect(confirmMock).toHaveBeenCalledTimes(1);
		expect(confirmMock.mock.calls[0][0]).toMatchObject({ danger: true });
		expect(api.resetOnboarding).toHaveBeenCalledWith(true);
		// The success reload to /jarvis/onboarding is exercised in the browser
		// flow review — jsdom makes window.location.assign non-stubbable.
	});
});
