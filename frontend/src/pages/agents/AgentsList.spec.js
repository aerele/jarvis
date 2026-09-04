import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 polish: a plain (non-admin, non-reviewer) user's catalog is
 * deny-by-default (see @/lib/agentsEmptyState.js), so two of the four tabs -
 * Featured and Installed - point at nothing for them. They get one tab that
 * matters (the Available catalog, relabelled "Agents") plus Activity; an
 * admin or a reviewer (the same caps probeCaps() already reads for the Apply
 * button / empty-state copy) keeps the full four-tab set unchanged.
 */

const api = vi.hoisted(() => ({
	listAgents: vi.fn(),
	getAgentsSyncStatus: vi.fn(),
	applyAgents: vi.fn(),
}));
vi.mock("@/api", () => api);

const agentsApi = vi.hoisted(() => ({
	listAgentsPage: vi.fn(),
	getAgentsCaps: vi.fn(),
}));
vi.mock("@/api/agents", () => agentsApi);

const router = vi.hoisted(() => ({ push: vi.fn() }));
const route = vi.hoisted(() => ({ hash: "", name: "AgentsList", query: {}, params: {} }));
// jarvis#1062 E2E defect: AgentsList.vue no longer imports onBeforeRouteLeave
// (the in-SPA leave-confirm dialog was removed, along with the beforeunload
// listener - see the no-leave-guard describe block below).
vi.mock("vue-router", () => ({
	useRouter: () => router,
	useRoute: () => route,
}));

// jsdom here has no usable localStorage, and useListPage's persisted view
// state is beside the point for this test (see WikiTab.spec.js precedent).
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	toast: { success: vi.fn(), error: vi.fn() },
	confirmDialog: vi.fn(),
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: "<div />" },
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		template: `<div v-if="modelValue"><slot name="body-content" /><slot name="actions" /></div>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"></select>`,
	},
	ListFooter: { name: "ListFooter", template: "<div />" },
}));

vi.mock("@/components/LayoutHeader.vue", () => ({
	default: {
		name: "LayoutHeader",
		template: `<div><slot name="left-header" /><slot name="right-header" /><slot /></div>`,
	},
}));
vi.mock("./AgentActivityTab.vue", () => ({
	default: { name: "AgentActivityTab", template: "<div>activity feed</div>" },
}));

import AgentsList from "./AgentsList.vue";

function tabButtons(w) {
	return w.findAll('[role="tab"]');
}
function tabLabels(w) {
	return tabButtons(w).map((b) => b.text());
}
function selectedTabLabel(w) {
	const sel = tabButtons(w).filter((b) => b.attributes("aria-selected") === "true");
	return sel.length ? sel[0].text() : null;
}

async function mountList({ caps = {}, hash = "", syncStatus = null, rows = [] } = {}) {
	route.hash = hash;
	agentsApi.getAgentsCaps.mockResolvedValue(caps);
	agentsApi.listAgentsPage.mockResolvedValue({ rows, total: rows.length, has_more: false });
	api.listAgents.mockResolvedValue([]);
	api.getAgentsSyncStatus.mockResolvedValue(
		syncStatus || { pending: false, dirty: false, status: "" }
	);
	const w = mount(AgentsList);
	await flushPromises();
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("a plain user (no review/admin caps) sees only Agents + Activity", () => {
	it("tabs are exactly [Agents, Activity]", async () => {
		const w = await mountList({ caps: { review: false, admin: false } });
		expect(tabLabels(w)).toEqual(["Agents", "Activity"]);
	});

	it("lands on the Agents tab by default (no hash)", async () => {
		const w = await mountList({ caps: { review: false, admin: false } });
		expect(selectedTabLabel(w)).toBe("Agents");
	});

	it("falls back to Agents when deep-linked to an admin-only tab (#installed)", async () => {
		const w = await mountList({ caps: { review: false, admin: false }, hash: "#installed" });
		expect(tabLabels(w)).toEqual(["Agents", "Activity"]);
		expect(selectedTabLabel(w)).toBe("Agents");
	});

	it("shows the no-access empty state on the Agents tab when nothing is granted", async () => {
		// wholeCatalogEmpty is probed lazily off an "available" page-1 fetch;
		// every listAgentsPage call in this test returns total 0, so it resolves
		// true and the deny-by-default copy wins over the generic per-tab one.
		const w = await mountList({ caps: { review: false, admin: false } });
		await flushPromises();
		expect(w.text()).toContain("No agents available to you");
		expect(w.text()).toContain("No agents have been made available to you yet");
	});
});

describe("an admin keeps the full four-tab catalog", () => {
	it("tabs are the unchanged full set", async () => {
		const w = await mountList({ caps: { review: false, admin: true } });
		expect(tabLabels(w)).toEqual(["Featured", "Available", "Installed", "Activity"]);
	});

	it("lands on Featured by default (no hash)", async () => {
		const w = await mountList({ caps: { review: false, admin: true } });
		expect(selectedTabLabel(w)).toBe("Featured");
	});

	it("a reviewer (not admin) also keeps the full four-tab set", async () => {
		const w = await mountList({ caps: { review: true, admin: false } });
		expect(tabLabels(w)).toEqual(["Featured", "Available", "Installed", "Activity"]);
	});
});

// jarvis#1062 E2E defect: a beforeunload listener used to fire the native
// "leave site?" prompt on ordinary SPA navigation whenever the catalog was
// dirty (canApply && sync.dirty). Removed outright - nothing is lost by
// navigating, since the dirty state lives server-side - while the "Changes
// pending" badge (the honest signal) stays.
describe("no leave-guard of any kind, even when the catalog is dirty (only the badge)", () => {
	it("never registers a beforeunload listener for a reviewer with unapplied changes", async () => {
		const addSpy = vi.spyOn(window, "addEventListener");
		const w = await mountList({
			caps: { review: true, admin: false },
			syncStatus: { pending: false, dirty: true, status: "" },
		});
		await flushPromises();

		expect(w.text()).toContain("Changes pending");
		expect(addSpy.mock.calls.some(([event]) => event === "beforeunload")).toBe(false);

		addSpy.mockRestore();
	});

	it("never registers one for a non-reviewer either (canApply false)", async () => {
		const addSpy = vi.spyOn(window, "addEventListener");
		await mountList({
			caps: { review: false, admin: false },
			syncStatus: { pending: false, dirty: true, status: "" },
		});
		await flushPromises();

		expect(addSpy.mock.calls.some(([event]) => event === "beforeunload")).toBe(false);

		addSpy.mockRestore();
	});

	it("renders no leave-confirm Dialog while dirty - the badge is the only signal", async () => {
		const w = await mountList({
			caps: { review: true, admin: false },
			syncStatus: { pending: false, dirty: true, status: "" },
		});
		await flushPromises();

		expect(w.text()).toContain("Changes pending");
		expect(w.text()).not.toContain("Unapplied catalog changes");
		expect(w.text()).not.toContain("Leave anyway");
		expect(w.find('[data-label="Leave anyway"]').exists()).toBe(false);
	});
});

// jarvis#1062 P1-4 (production-readiness audit): a long agent title used to
// be cut to one truncated line with no way to read the rest.
describe("catalog card titles: two lines, full name in a title attribute", () => {
	function longTitleAgent(overrides = {}) {
		return {
			agent_slug: "long-title-agent",
			title: "Statutory Payroll Deposit Status & Compliance Cross-Checking Auditor",
			status: "Published",
			publisher: "Aerele",
			version: "1.0.0",
			description: "Checks payroll deposits.",
			install_count: 0,
			...overrides,
		};
	}

	it("renders with line-clamp-2, not a single-line truncate", async () => {
		const w = await mountList({
			caps: { review: true, admin: false },
			rows: [longTitleAgent()],
		});
		const title = w.findAll("span").find((s) => s.text() === longTitleAgent().title);
		expect(title).toBeTruthy();
		expect(title.classes()).toContain("line-clamp-2");
		expect(title.classes()).not.toContain("truncate");
	});

	it("carries the full title in a title attribute for anything two lines still cannot fit", async () => {
		const w = await mountList({
			caps: { review: true, admin: false },
			rows: [longTitleAgent()],
		});
		const title = w.findAll("span").find((s) => s.text() === longTitleAgent().title);
		expect(title.attributes("title")).toBe(longTitleAgent().title);
	});
});

// jarvis#1062 P1-5 (production-readiness audit): a plain role="button" div
// carries no default browser focus outline the way a real <button> would.
describe("catalog card keyboard focus (jarvis#1062 P1-5)", () => {
	it("every card carries a focus-visible ring", async () => {
		const w = await mountList({
			caps: { review: true, admin: false },
			rows: [
				{
					agent_slug: "close-auditor",
					title: "Close Auditor",
					status: "Published",
					publisher: "Aerele",
					version: "1.0.0",
					description: "Checks the close.",
					install_count: 3,
				},
			],
		});
		const card = w.find('[role="button"]');
		expect(card.exists()).toBe(true);
		expect(card.classes()).toContain("focus-visible:ring-2");
		expect(card.classes()).toContain("focus-visible:ring-outline-gray-3");
	});
});

// jarvis#1062 P2-9 (production-readiness audit): wired through to the real
// component - agentsEmptyState.spec.js covers the pure-function decision.
describe("Installed tab empty state names Administrator (jarvis#1062 P2-9)", () => {
	it("shows the Administrator-specific copy, no install CTA", async () => {
		const { session } = await import("@/data/session");
		const original = session.user;
		session.user = "Administrator";
		try {
			const w = await mountList({
				caps: { review: false, admin: true },
				hash: "#installed",
			});
			expect(w.text()).toContain("Administrator cannot install agents.");
			expect(w.text()).toContain("Sign in as a named user.");
		} finally {
			session.user = original;
		}
	});
});
