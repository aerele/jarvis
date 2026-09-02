import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * AgentAccessEditor: the four behaviours that decide whether "Save access"
 * means what an admin thinks it means.
 *
 *   1. The button is inert until something actually changed, and the unsaved
 *      marker appears exactly when it is. The old two-step flow had a live Save
 *      button and no marker, so an admin could not tell a saved list from a
 *      half-edited one.
 *   2. ONE save carries BOTH lists. Roles and users are two halves of one
 *      access statement; saving them separately means passing through an access
 *      state nobody chose.
 *   3. The apply checkbox is what decides whether the workspace restarts, and
 *      it is the ADMIN's choice - the toast differs so they know which of the
 *      two things just happened.
 *   4. A pending apply is polled to a terminal state, and a FAILED one says why
 *      rather than reporting success.
 */

const agentsApi = vi.hoisted(() => ({
	setAgentAccess: vi.fn(),
	searchUsers: vi.fn(),
}));
vi.mock("@/api/agents", () => agentsApi);

const api = vi.hoisted(() => ({ getAgentsSyncStatus: vi.fn() }));
vi.mock("@/api", () => api);

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js).
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("frappe-ui", () => ({
	toast,
	call: vi.fn(),
	Button: {
		name: "Button",
		props: ["label", "loading", "disabled", "variant", "icon"],
		emits: ["click"],
		template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
	},
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder"],
		emits: ["update:modelValue"],
		template: "<div class='autocomplete'></div>",
	},
}));
vi.mock("@/components/JvCombo.vue", () => ({
	default: {
		name: "JvCombo",
		props: ["modelValue", "options", "allowCustom", "placeholder"],
		emits: ["update:modelValue"],
		template: "<div class='jvcombo'></div>",
	},
}));
vi.mock("@/lib/errors", () => ({ errHtml: (e) => String((e && e.message) || e) }));

import AgentAccessEditor from "./AgentAccessEditor.vue";

const SAVED_ROLES = ["Accounts User"];
const SAVED_USERS = ["ann@example.com"];

function editor(props = {}) {
	return mount(AgentAccessEditor, {
		props: {
			slug: "close-auditor",
			roles: [...SAVED_ROLES],
			users: [...SAVED_USERS],
			allRoles: ["Accounts User", "Accounts Manager", "Stock User"],
			...props,
		},
	});
}

const saveBtn = (w) => w.find('[data-test="save-access"]');

beforeEach(() => {
	vi.useFakeTimers();
	agentsApi.setAgentAccess.mockReset();
	agentsApi.searchUsers.mockReset().mockResolvedValue([]);
	api.getAgentsSyncStatus.mockReset();
	toast.success.mockReset();
	toast.error.mockReset();
	// Default: the save lands and reports back exactly what the draft asked for.
	agentsApi.setAgentAccess.mockImplementation(async (slug, roles, users, apply) => ({
		ok: true,
		allowed_roles: roles,
		allowed_users: users,
		applied: !!apply,
	}));
});
afterEach(() => {
	vi.useRealTimers();
});

describe("dirty state", () => {
	it("starts clean: no marker, save disabled", () => {
		const w = editor();
		expect(w.find('[data-test="access-dirty"]').exists()).toBe(false);
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("marks dirty and enables save once a role is removed", async () => {
		const w = editor();
		w.vm.roleDraft = [];
		await flushPromises();
		expect(w.find('[data-test="access-dirty"]').exists()).toBe(true);
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
	});

	it("a change that nets out to the saved value is NOT dirty", async () => {
		// Order must not count as a change: the server returns its own order and an
		// admin who removes and re-adds the same role has changed nothing.
		const w = editor({ roles: ["A", "B"], users: [] });
		w.vm.roleDraft = ["B", "A"];
		await flushPromises();
		expect(w.find('[data-test="access-dirty"]').exists()).toBe(false);
	});

	it("goes clean again after a successful save", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({ pending: false, last_sync_status: "ok" });
		const w = editor();
		w.vm.roleDraft = ["Stock User"];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(w.find('[data-test="access-dirty"]').exists()).toBe(false);
	});
});

describe("one save carries both lists", () => {
	it("sends roles AND users in a single call", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({ pending: false, last_sync_status: "ok" });
		const w = editor();
		w.vm.roleDraft = ["Stock User"];
		w.vm.userDraft = ["bo@example.com"];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(agentsApi.setAgentAccess).toHaveBeenCalledTimes(1);
		const [slug, roles, users] = agentsApi.setAgentAccess.mock.calls[0];
		expect(slug).toBe("close-auditor");
		expect(roles).toEqual(["Stock User"]);
		expect(users).toEqual(["bo@example.com"]);
	});

	it("emits the saved value so the page can update without a refetch", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({ pending: false, last_sync_status: "ok" });
		const w = editor();
		w.vm.userDraft = [];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(w.emitted("saved")[0][0]).toEqual({
			allowed_roles: SAVED_ROLES,
			allowed_users: [],
		});
	});

	it("saving an empty pair is allowed - it CLOSES the agent, it does not open it", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({ pending: false, last_sync_status: "ok" });
		const w = editor();
		w.vm.roleDraft = [];
		w.vm.userDraft = [];
		await flushPromises();
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
		await saveBtn(w).trigger("click");
		await flushPromises();
		const [, roles, users] = agentsApi.setAgentAccess.mock.calls[0];
		expect(roles).toEqual([]);
		expect(users).toEqual([]);
	});
});

describe("apply checkbox", () => {
	it("defaults ON and asks the server to apply", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({ pending: false, last_sync_status: "ok" });
		const w = editor();
		expect(w.find('[data-test="apply-now"]').element.checked).toBe(true);
		w.vm.roleDraft = [];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(agentsApi.setAgentAccess.mock.calls[0][3]).toBe(true);
	});

	it("unchecked: no apply, and the toast says the change is not live yet", async () => {
		const w = editor();
		await w.find('[data-test="apply-now"]').setValue(false);
		w.vm.roleDraft = [];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(agentsApi.setAgentAccess.mock.calls[0][3]).toBe(false);
		expect(api.getAgentsSyncStatus).not.toHaveBeenCalled();
		expect(toast.success).toHaveBeenCalledWith(
			"Access saved. Apply catalog changes to make it runnable."
		);
	});
});

describe("apply progress", () => {
	it("shows the pending badge, polls, then reports success", async () => {
		api.getAgentsSyncStatus
			.mockResolvedValueOnce({ pending: true })
			.mockResolvedValueOnce({ pending: false, last_sync_status: "ok" });
		const w = editor();
		w.vm.roleDraft = [];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(w.find('[data-test="apply-pending"]').exists()).toBe(true);
		expect(toast.success).not.toHaveBeenCalled();

		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();

		expect(w.find('[data-test="apply-pending"]').exists()).toBe(false);
		expect(toast.success).toHaveBeenCalledWith("Access saved and applied.");
	});

	it("a failed apply surfaces the reason, never a success toast", async () => {
		api.getAgentsSyncStatus.mockResolvedValue({
			pending: false,
			last_sync_status: "failed: workspace refused the roster",
		});
		const w = editor();
		w.vm.roleDraft = [];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(toast.error).toHaveBeenCalledWith("workspace refused the roster");
		expect(toast.success).not.toHaveBeenCalled();
		expect(w.find('[data-test="apply-pending"]').exists()).toBe(false);
	});

	it("a rejected save leaves the draft intact so the edit is not lost", async () => {
		agentsApi.setAgentAccess.mockRejectedValue(new Error("You need the Jarvis Admin role"));
		const w = editor();
		w.vm.roleDraft = ["Stock User"];
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(toast.error).toHaveBeenCalledWith("You need the Jarvis Admin role");
		expect(w.vm.roleDraft).toEqual(["Stock User"]);
		expect(w.find('[data-test="access-dirty"]').exists()).toBe(true);
	});
});
