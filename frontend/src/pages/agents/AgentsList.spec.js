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
vi.mock("vue-router", () => ({
	useRouter: () => router,
	useRoute: () => route,
	onBeforeRouteLeave: vi.fn(),
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

async function mountList({ caps = {}, hash = "" } = {}) {
	route.hash = hash;
	agentsApi.getAgentsCaps.mockResolvedValue(caps);
	agentsApi.listAgentsPage.mockResolvedValue({ rows: [], total: 0, has_more: false });
	api.listAgents.mockResolvedValue([]);
	api.getAgentsSyncStatus.mockResolvedValue({ pending: false, dirty: false, status: "" });
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
