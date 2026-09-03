import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createRouter, createMemoryHistory, RouterView } from "vue-router";
import { h } from "vue";

/**
 * An unsaved Access draft must outlive the two ways it used to disappear.
 *
 *   1. A TAB switch. The Admin panel was the last arm of the tabs' v-if chain,
 *      so Overview and back unmounted the editor and silently threw away a
 *      half-finished grant. It is now hidden with v-show instead.
 *   2. A ROUTE change. Leaving the agent page took the draft with it and said
 *      nothing; there is now one confirmation, and only when there is really
 *      something to lose.
 *
 * Mounted through a real memory router on purpose: onBeforeRouteLeave only
 * registers for a component the router itself matched, so a direct mount would
 * test nothing. Everything below AgentDetail is stubbed - the subject here is
 * the page's own retention behaviour, not its children.
 */

const api = vi.hoisted(() => ({
	installAgent: vi.fn(),
	uninstallAgent: vi.fn(),
	setEnabled: vi.fn(),
	setSchedule: vi.fn(),
	getAgentAdminOverview: vi.fn(),
	applyAgents: vi.fn(),
	getAgentsSyncStatus: vi.fn(),
	setListingStatus: vi.fn(),
}));
vi.mock("@/api", () => api);

const apiAgents = vi.hoisted(() => ({
	getAgent: vi.fn(),
	getAgentsCaps: vi.fn(),
	getInstallationActivation: vi.fn(),
	stopAgentRun: vi.fn(),
	promoteInstallation: vi.fn(),
	demoteInstallation: vi.fn(),
	setAgentAccess: vi.fn(),
	searchUsers: vi.fn(),
	listRunsPage: vi.fn(),
	listAgentActivityPage: vi.fn(),
	takeFindingToChat: vi.fn(),
	listAgentsPage: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

const confirmDialog = vi.hoisted(() => vi.fn());
vi.mock("frappe-ui", () => {
	const passthrough = (name) => ({
		name,
		inheritAttrs: false,
		setup:
			(_, { slots }) =>
			() =>
				h("div", {}, slots.default ? slots.default() : []),
	});
	return {
		confirmDialog,
		toast: { success: vi.fn(), error: vi.fn() },
		call: vi.fn(),
		Badge: passthrough("Badge"),
		Breadcrumbs: passthrough("Breadcrumbs"),
		Button: passthrough("Button"),
		Dropdown: passthrough("Dropdown"),
		FeatherIcon: passthrough("FeatherIcon"),
		FormControl: passthrough("FormControl"),
		FormLabel: passthrough("FormLabel"),
		ListView: passthrough("ListView"),
		ListHeader: passthrough("ListHeader"),
		ListHeaderItem: passthrough("ListHeaderItem"),
		ListRows: passthrough("ListRows"),
		ListRowItem: passthrough("ListRowItem"),
		Switch: passthrough("Switch"),
		TimePicker: passthrough("TimePicker"),
		Tooltip: passthrough("Tooltip"),
		Autocomplete: passthrough("Autocomplete"),
	};
});

// The editor stub exposes `dirty` exactly as the real one does (defineExpose),
// which is the only part of it this page reads.
const editorState = vi.hoisted(() => ({ dirty: false }));
vi.mock("@/pages/agents/AgentAccessEditor.vue", () => ({
	default: {
		name: "AgentAccessEditor",
		setup(_, { expose }) {
			expose(editorState);
			return () => h("div", { class: "access-editor-stub" });
		},
	},
}));

vi.mock("@/markdown", () => ({ renderMarkdown: (s) => s || "" }));
vi.mock("@/lib/errors", () => ({ errMessage: (e) => String(e), errHtml: (e) => String(e) }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "", exactDate: () => "" }));
vi.mock("@/composables/useDocmeta", () => ({ useDocmeta: () => ({ meta: { value: null } }) }));

import AgentDetail from "./AgentDetail.vue";

const SLUG = "close-auditor";
const STUBS = {
	LayoutHeader: true,
	TabBar: true,
	CommentsSection: true,
	AgentRunsBoard: true,
	ActivationPanel: true,
	ConfigForm: true,
	AppSourceConsentDialog: true,
	JvSpinner: true,
	ShadowChip: true,
};

async function page() {
	const router = createRouter({
		history: createMemoryHistory(),
		routes: [
			{ path: "/agents/:slug", name: "AgentDetail", component: AgentDetail, props: true },
			{
				path: "/elsewhere",
				name: "Elsewhere",
				component: { template: "<div>elsewhere</div>" },
			},
		],
	});
	router.push(`/agents/${SLUG}`);
	await router.isReady();
	const w = mount(
		{ render: () => h(RouterView) },
		{ global: { plugins: [router], stubs: STUBS } }
	);
	await flushPromises();
	return { w, router };
}

beforeEach(() => {
	editorState.dirty = false;
	confirmDialog.mockReset();
	apiAgents.getAgent.mockResolvedValue({
		name: SLUG,
		agent_slug: SLUG,
		title: "Close Auditor",
		status: "Published",
		nature: "Auditor",
		allowed: 1,
		allowed_roles: [],
		allowed_users: [],
		all_roles: ["Accounts User"], // presence of all_roles is the isSM signal
		installation: null,
		install_count: 0,
	});
	apiAgents.getAgentsCaps.mockResolvedValue({ review: true, admin: true });
	api.getAgentAdminOverview.mockResolvedValue({ roles: [], listings: [] });
});

describe("the Access draft survives a tab switch", () => {
	it("keeps the Admin panel mounted when another tab is shown", async () => {
		const { w, router } = await page();

		router.push("#admin");
		await flushPromises();
		expect(w.find(".access-editor-stub").exists()).toBe(true);

		// Overview and back: with the old v-if chain the editor unmounted here and
		// its draft went with it.
		router.push("#");
		await flushPromises();
		expect(w.find(".access-editor-stub").exists()).toBe(true);

		router.push("#admin");
		await flushPromises();
		expect(w.find(".access-editor-stub").exists()).toBe(true);
	});
});

describe("leaving the page with an unsaved draft", () => {
	it("asks once, and only once", async () => {
		const { router } = await page();
		editorState.dirty = true;

		await router.push("/elsewhere");
		await flushPromises();

		expect(confirmDialog).toHaveBeenCalledTimes(1);
		// Blocked for now - the user is still on the agent page.
		expect(router.currentRoute.value.path).toBe(`/agents/${SLUG}`);

		// Confirming lets the SAME navigation through without asking again.
		confirmDialog.mock.calls[0][0].onConfirm({ hideDialog: () => {} });
		await flushPromises();
		expect(confirmDialog).toHaveBeenCalledTimes(1);
		expect(router.currentRoute.value.path).toBe("/elsewhere");
	});

	it("does not ask when the draft is clean", async () => {
		const { router } = await page();
		editorState.dirty = false;

		await router.push("/elsewhere");
		await flushPromises();

		expect(confirmDialog).not.toHaveBeenCalled();
		expect(router.currentRoute.value.path).toBe("/elsewhere");
	});

	it("dismissing the dialog leaves routing usable, not wedged", async () => {
		// The guard refuses the navigation outright rather than parking a `next`
		// callback: a dismissed dialog never calls onConfirm, and a guard waiting on
		// that callback would wedge routing for the rest of the session.
		const { router } = await page();
		editorState.dirty = true;

		await router.push("/elsewhere");
		await flushPromises();
		expect(router.currentRoute.value.path).toBe(`/agents/${SLUG}`);

		// No confirm, no re-push: the draft is saved, and leaving now works.
		editorState.dirty = false;
		await router.push("/elsewhere");
		await flushPromises();
		expect(router.currentRoute.value.path).toBe("/elsewhere");
	});
});
