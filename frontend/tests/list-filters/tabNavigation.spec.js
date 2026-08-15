import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createRouter, createMemoryHistory } from "vue-router";
import { frappeUiStubs } from "./stubs.js";

/**
 * Tab switches must not destroy the list's filter set.
 *
 * These are BEHAVIOURAL on purpose. The obvious pin — grep the page for
 * "route.query" — passes on the broken code, because the defect was
 * `v === "activity" ? route.query : {}`: the string is right there on one leg
 * while the other leg replaces the whole query with `{}`. A reviewer reading
 * that line cleared it. So these drive a REAL vue-router through the round trip
 * and assert on where `fv2` actually ends up.
 *
 * Both pages own a per-tab deep-link key that SHOULD be dropped when leaving its
 * tab (Activity's `?trigger=`, Builder's `?edit=`), so each test asserts the
 * narrow thing is dropped AND the filter set is not.
 */

const toastDouble = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), create: vi.fn() }));
vi.mock("frappe-ui", () => ({ ...frappeUiStubs(), toast: toastDouble }));

// Every child pane is inert here: this is about the parent's navigation.
// Declared with vi.hoisted so the mock factories (hoisted above all consts) can
// close over them.
const { TAB_BAR, INERT } = vi.hoisted(() => ({
	TAB_BAR: {
		name: "TabBar",
		props: ["tabs", "modelValue"],
		emits: ["update:modelValue"],
		template: `<div class="tabbar">
			<button v-for="t in tabs" :key="t.value" :data-tab="t.value"
				@click="$emit('update:modelValue', t.value)">{{ t.label }}</button>
		</div>`,
	},
	INERT: { template: "<div/>" },
}));

vi.mock("@/components/list/TabBar.vue", () => ({ default: TAB_BAR }));
vi.mock("@/components/LayoutHeader.vue", () => ({ default: { template: "<div><slot/></div>" } }));
vi.mock("@/pages/triggers/TriggersListPane.vue", () => ({ default: INERT }));
vi.mock("@/pages/triggers/ActivityTab.vue", () => ({ default: INERT }));
vi.mock("@/pages/dashboards/DashboardCanvas.vue", () => ({ default: INERT }));
vi.mock("@/pages/dashboards/DashboardChatPane.vue", () => ({ default: INERT }));
vi.mock("@/pages/dashboards/SavedDashboardsTab.vue", () => ({ default: INERT }));
vi.mock("@/pages/dashboards/SaveDashboardDialog.vue", () => ({ default: INERT }));

vi.mock("@/api/triggers", () => ({
	getTriggersCaps: vi.fn(async () => ({
		can_manage: true,
		scripts_enabled: false,
		stt_enabled: false,
		events: [],
		llm_events: [],
	})),
}));
vi.mock("@/api/dashboards", () => ({
	getDashboardsCaps: vi.fn(async () => ({ can_manage: true })),
	getDashboard: vi.fn(async () => ({})),
	getDashboardConversation: vi.fn(async () => ({})),
	listDashboardsPage: vi.fn(async () => ({ ok: true, data: { rows: [], total: 0 } })),
}));
vi.mock("@/api", () => ({
	getCanvas: vi.fn(async () => ({})),
	searchLink: vi.fn(async () => []),
}));
vi.mock("@/data/session", () => ({ session: { user: "u@x.com" }, sessionUser: "u@x.com" }));
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (k, v) => ref(v) };
});
vi.mock("@/lib/dashboardRestore", () => ({ builderCanvasFrame: () => "" }));

import TriggersPage from "@/pages/triggers/TriggersPage.vue";
import DashboardsPage from "@/pages/dashboards/DashboardsPage.vue";

/** A real router — the whole point is that vue-router resolves the location. */
function makeRouter(path) {
	const router = createRouter({
		history: createMemoryHistory(),
		routes: [
			{ path: "/triggers", name: "TriggersPage", component: { template: "<div/>" } },
			{ path: "/dashboards", name: "DashboardsPage", component: { template: "<div/>" } },
		],
	});
	return router.push(path).then(() => router);
}

const FV2 = JSON.stringify({
	v: 1,
	k: "triggers",
	c: [["Jarvis Trigger", "trigger_name", "like", "invoice"]],
});

beforeEach(() => vi.clearAllMocks());

describe("TriggersPage: Triggers → Activity → Triggers keeps the filter set", () => {
	async function mountAt(query) {
		const router = await makeRouter({ path: "/triggers", query });
		const w = mount(TriggersPage, { global: { plugins: [router] } });
		await flushPromises();
		return { w, router };
	}

	it("round-trips fv2 through the Activity tab", async () => {
		const { w, router } = await mountAt({ fv2: FV2 });
		expect(router.currentRoute.value.query.fv2).toBe(FV2);

		await w.find('[data-tab="activity"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.hash).toBe("#activity");
		expect(router.currentRoute.value.query.fv2).toBe(FV2);

		await w.find('[data-tab="triggers"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.hash).toBe("");
		expect(router.currentRoute.value.query.fv2).toBe(FV2);
	});

	it("still drops Activity's own ?trigger= deep-link on the way back", async () => {
		const { w, router } = await mountAt({ fv2: FV2, trigger: "TRG-0001" });
		await w.find('[data-tab="activity"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.trigger).toBe("TRG-0001");

		await w.find('[data-tab="triggers"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.trigger).toBeUndefined();
		expect(router.currentRoute.value.query.fv2).toBe(FV2);
	});

	it("carries any other query key both ways", async () => {
		const { w, router } = await mountAt({ fv2: FV2, ref: "keep-me" });
		await w.find('[data-tab="activity"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.ref).toBe("keep-me");
		await w.find('[data-tab="triggers"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.ref).toBe("keep-me");
	});
});

describe("DashboardsPage: Builder ⇄ Saved keeps the filter set", () => {
	const DASH_FV2 = JSON.stringify({
		v: 1,
		k: "saved_dashboards",
		c: [["Jarvis Dashboard", "dashboard_title", "like", "q3"]],
	});

	async function mountAt(query, hash = "#saved") {
		const router = await makeRouter({ path: "/dashboards", query, hash });
		const w = mount(DashboardsPage, { global: { plugins: [router] } });
		await flushPromises();
		return { w, router };
	}

	it("round-trips fv2 through the Builder tab", async () => {
		const { w, router } = await mountAt({ fv2: DASH_FV2 });
		expect(router.currentRoute.value.query.fv2).toBe(DASH_FV2);

		await w.find('[data-tab="builder"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.fv2).toBe(DASH_FV2);

		await w.find('[data-tab="saved"]').trigger("click");
		await flushPromises();
		expect(router.currentRoute.value.query.fv2).toBe(DASH_FV2);
	});
});
