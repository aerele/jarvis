import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 polish - AgentDetail.vue:
 *
 *   5. Visible (not just tooltip) hints for the two other reasons Run Now can
 *      be disabled - enabled-off and shadow-scribe - mirroring the existing
 *      role-restricted hint.
 *   6. doctypes_required rendered as "Reads these records" chips, and the
 *      install-failure reason kept visible under the Install button (not only
 *      a 2s toast) until the next attempt.
 *   3. The Overview "Allowed roles" panel renders only when the payload
 *      carries the (now admin-only) allowed_roles key.
 */

const api = vi.hoisted(() => ({
	installAgent: vi.fn(),
	runAgentNow: vi.fn(),
	setAgentConfig: vi.fn(),
	setAgentEnabled: vi.fn(),
	setAgentRoles: vi.fn(),
	setAgentSchedule: vi.fn(),
	uninstallAgent: vi.fn(),
	getAgentAdminOverview: vi.fn(),
}));
vi.mock("@/api", () => api);

const apiAgents = vi.hoisted(() => ({
	getAgent: vi.fn(),
	getInstallationActivation: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

const apiDocmeta = vi.hoisted(() => ({
	getDocmeta: vi.fn().mockResolvedValue(null),
}));
vi.mock("@/api/docmeta", () => apiDocmeta);

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({
	useRouter: () => router,
	useRoute: () => ({ hash: "", name: "AgentDetail", query: {}, params: {} }),
}));

vi.mock("@/data/session", () => ({ session: { user: "owner@example.com" } }));

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js).
vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: (d) => ({
		format: () => String(d || ""),
		fromNow: () => "",
		isValid: () => !!d,
		valueOf: () => (d ? new Date(String(d).replace(" ", "T")).getTime() : 0),
	}),
	getConfig: () => null,
	toast: {
		success: vi.fn(),
		error: vi.fn(),
		// mirrors real toast.promise's shape closely enough: attach a swallowed
		// .catch so the component's own await/catch is the only thing under test.
		promise: vi.fn((p) => {
			p.catch(() => {});
			return p;
		}),
	},
	confirmDialog: vi.fn(),
	Autocomplete: { name: "Autocomplete", template: "<div />" },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: "<div />" },
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "theme", "iconLeft", "tooltip"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dropdown: { name: "Dropdown", props: ["options"], template: "<div><slot /></div>" },
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"></select>`,
	},
	FormLabel: { name: "FormLabel", template: "<label><slot /></label>" },
	ListView: { name: "ListView", template: "<div />" },
	ListHeader: { name: "ListHeader", template: "<div />" },
	ListHeaderItem: { name: "ListHeaderItem", template: "<div />" },
	ListRows: { name: "ListRows", template: "<div />" },
	ListRowItem: { name: "ListRowItem", template: "<div />" },
	Switch: {
		name: "Switch",
		props: ["modelValue", "label", "disabled"],
		emits: ["update:modelValue"],
		template: `<button :disabled="disabled" @click="$emit('update:modelValue', !modelValue)">{{ label }}</button>`,
	},
	TimePicker: { name: "TimePicker", template: "<div />" },
}));

// LayoutHeader teleports to #app-header (absent in jsdom here) - a real mount
// would render nothing at all, hiding the Install/Run Now buttons under test.
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: {
		name: "LayoutHeader",
		template: `<div><slot name="left-header" /><slot name="right-header" /><slot /></div>`,
	},
}));
vi.mock("@/components/list/TabBar.vue", () => ({
	default: { name: "TabBar", props: ["tabs", "modelValue"], template: "<div />" },
}));
vi.mock("@/components/doc/CommentsSection.vue", () => ({
	default: { name: "CommentsSection", template: "<div />" },
}));
vi.mock("@/pages/agents/AgentRunsBoard.vue", () => ({
	default: { name: "AgentRunsBoard", template: "<div />" },
}));
vi.mock("@/pages/agents/ActivationPanel.vue", () => ({
	default: { name: "ActivationPanel", template: "<div />" },
}));
vi.mock("@/pages/agents/ConfigForm.vue", () => ({
	default: { name: "ConfigForm", template: "<div />" },
}));
vi.mock("@/components/learning/AppSourceConsentDialog.vue", () => ({
	default: { name: "AppSourceConsentDialog", template: "<div />" },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
vi.mock("@/markdown", () => ({ renderMarkdown: (s) => s }));
vi.mock("@/lib/errors", () => ({
	errMessage: (e) => (e && e.message) || String(e),
	errHtml: (e) => (e && e.message) || String(e),
}));

import AgentDetail from "./AgentDetail.vue";

function baseAgent(overrides = {}) {
	return {
		name: "close-auditor",
		agent_slug: "close-auditor",
		title: "Close Auditor",
		description: "Checks the close.",
		category: "Close and Reporting",
		nature: "Auditor",
		version: "1.0.0",
		publisher: "Jarvis",
		status: "Published",
		tools_required: "[]",
		min_apps: "[]",
		rule_pack: "pack-1",
		default_schedule: "{}",
		validated_for_fy: "",
		allowed_roles: [],
		doctypes_required: "[]",
		allowed: 1,
		install_count: 3,
		installation: null,
		...overrides,
	};
}

function installedInstallation(overrides = {}) {
	return {
		name: "INST-1",
		enabled: 1,
		installed_version: "1.0.0",
		installed_at: "2026-01-01 00:00:00",
		config: "{}",
		sync_status: "",
		synced_at: "",
		schedule_enabled: 0,
		schedule_frequency: "",
		schedule_time: null,
		next_run_at: null,
		last_run_at: null,
		...overrides,
	};
}

async function mountDetail(agentFixture, activation = null) {
	apiAgents.getAgent.mockResolvedValue(agentFixture);
	apiAgents.getInstallationActivation.mockResolvedValue(activation);
	const w = mount(AgentDetail, { props: { slug: agentFixture.agent_slug } });
	await flushPromises();
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("Run Now disabled reasons get a visible hint, not only a tooltip", () => {
	it("shows an enable-it hint when installed but not enabled", async () => {
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 0 }) })
		);
		expect(w.text()).toContain("Enable this agent to run it.");
	});

	it("does not show the enable-it hint once enabled", async () => {
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		expect(w.text()).not.toContain("Enable this agent to run it.");
	});

	it("shows the shadow-scribe hint (existing tooltip text) for a shadow scribe", async () => {
		const w = await mountDetail(
			baseAgent({
				nature: "Scribe",
				installation: installedInstallation({ enabled: 1 }),
			}),
			{ activation_state: "shadow", reviewer: "owner@example.com" }
		);
		await flushPromises();
		expect(w.text()).toContain(
			"Still in shadow preview - promote it to live under Configure first"
		);
	});
});

describe("doctypes_required renders as Reads these records chips", () => {
	it("lists each declared doctype as a chip", async () => {
		const w = await mountDetail(baseAgent({ doctypes_required: '["GL Entry","Account"]' }));
		expect(w.text()).toContain("Reads these records");
		expect(w.text()).toContain("GL Entry");
		expect(w.text()).toContain("Account");
	});

	it("omits the heading when nothing is declared", async () => {
		const w = await mountDetail(baseAgent({ doctypes_required: "[]" }));
		expect(w.text()).not.toContain("Reads these records");
	});
});

describe("install-failure reason stays visible until the next attempt", () => {
	it("shows the error inline under Install, not only via toast", async () => {
		api.installAgent.mockRejectedValueOnce(new Error("no seats left"));
		const w = await mountDetail(baseAgent({ installation: null }));
		const installBtn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Install");
		await installBtn.trigger("click");
		await flushPromises();
		expect(w.text()).toContain("no seats left");
	});

	it("clears the inline error on the next attempt", async () => {
		api.installAgent.mockRejectedValueOnce(new Error("no seats left"));
		const w = await mountDetail(baseAgent({ installation: null }));
		const installBtn = () =>
			w.findAll("button").find((b) => b.attributes("data-label") === "Install");
		await installBtn().trigger("click");
		await flushPromises();
		expect(w.text()).toContain("no seats left");

		api.installAgent.mockResolvedValueOnce({ ok: true });
		await installBtn().trigger("click");
		await flushPromises();
		expect(w.text()).not.toContain("no seats left");
	});
});

describe("Overview Access panel: roster for admins only, never for a non-admin (governance + #1062 polish)", () => {
	it("shows only the caller's own allowed/not-allowed state for a non-admin payload (no all_roles)", async () => {
		const fixture = baseAgent();
		delete fixture.allowed_roles;
		delete fixture.allowed_users;
		const w = await mountDetail(fixture);
		expect(w.text()).toContain("Access");
		expect(w.text()).toContain("You have access");
		// never the roster, even if a stray allowed_roles key somehow rode along -
		// isSM (all_roles presence), not key-presence, gates the roster now.
		expect(w.text()).not.toContain("Accounts User");
	});

	it("shows the access roster for an admin payload (all_roles present)", async () => {
		const w = await mountDetail(
			baseAgent({
				all_roles: ["Accounts User", "Jarvis Admin"],
				allowed_roles: ["Accounts User"],
				allowed_users: [],
			})
		);
		expect(w.text()).toContain("Access");
		expect(w.text()).toContain("Accounts User");
	});
});
