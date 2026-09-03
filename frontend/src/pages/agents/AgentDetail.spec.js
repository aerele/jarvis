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
const routeMock = vi.hoisted(() => ({ hash: "", name: "AgentDetail", query: {}, params: {} }));
vi.mock("vue-router", () => ({
	useRouter: () => router,
	useRoute: () => routeMock,
	// jarvis#1062 access-governance merge: AgentDetail.vue now also guards
	// leaving with an unsaved Access draft (onBeforeRouteLeave) - a no-op
	// here, since none of this file's tests dirty the Access editor.
	onBeforeRouteLeave: vi.fn(),
}));

const sessionMock = vi.hoisted(() => ({ session: { user: "owner@example.com" } }));
vi.mock("@/data/session", () => sessionMock);

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
	Breadcrumbs: {
		name: "Breadcrumbs",
		props: ["items"],
		// jarvis#1062 P1-6: render each item's label + its #suffix scoped slot
		// (the skeleton bar), matching frappe-ui's real Breadcrumbs API closely
		// enough to test the loading-crumb behavior.
		template: `<div>
			<span v-for="(item, i) in items" :key="i" class="crumb" :data-loading="!!item.loading">
				{{ item.label }}<slot name="suffix" :item="item" />
			</span>
		</div>`,
	},
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
	// emit-capable so a time-only edit can be driven in a spec (jarvis#1062
	// owner feedback: Save must hide again after a time-only save - the
	// modelValue TimePicker emits can carry seconds, e.g. "10:30:00").
	TimePicker: {
		name: "TimePicker",
		props: ["modelValue", "placeholder"],
		emits: ["update:modelValue"],
		template: `<button data-testid="time-picker" @click="$emit('update:modelValue', '10:30:00')">{{ modelValue }}</button>`,
	},
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
	sessionMock.session.user = "owner@example.com";
	routeMock.hash = "";
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

describe("Install is disabled for an Administrator session (jarvis#1062 polish)", () => {
	it("disables the Install button and shows the visible hint", async () => {
		sessionMock.session.user = "Administrator";
		const w = await mountDetail(baseAgent({ installation: null }));
		const installBtn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Install");
		expect(installBtn.attributes("disabled")).not.toBeUndefined();
		expect(w.text()).toContain("Log in as a named user to install this agent.");
	});

	it("a click while disabled never calls installAgent", async () => {
		sessionMock.session.user = "Administrator";
		const w = await mountDetail(baseAgent({ installation: null }));
		const installBtn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Install");
		await installBtn.trigger("click");
		await flushPromises();
		expect(api.installAgent).not.toHaveBeenCalled();
	});

	it("a named (non-Administrator) user sees no such hint and can install", async () => {
		const w = await mountDetail(baseAgent({ installation: null }));
		const installBtn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Install");
		expect(installBtn.attributes("disabled")).toBeUndefined();
		expect(w.text()).not.toContain("Log in as a named user to install this agent.");
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

describe("Configure tab: two-column layout on lg+ (jarvis#1062 polish, matches the Admin tab)", () => {
	it("wraps Configuration and Schedule in the same grid-cols-1 lg:grid-cols-2 pattern as Admin", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		const grid = w.find(".grid.grid-cols-1.gap-10.lg\\:grid-cols-2.lg\\:items-start");
		expect(grid.exists()).toBe(true);
		expect(grid.text()).toContain("Schedule");
		expect(grid.text()).toContain("Configuration");
	});

	// owner feedback: Configuration is the primary/left column now that
	// Comments moved off this tab - Schedule moved to the right. Asserted on
	// DOM order, not just presence, so a re-swap regresses visibly.
	it("Configuration is the LEFT/primary column, Schedule is the RIGHT column", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		const grid = w.find(".grid.grid-cols-1.gap-10.lg\\:grid-cols-2.lg\\:items-start");
		const headings = grid
			.findAll(".text-base.font-medium.text-ink-gray-9")
			.map((h) => h.text());
		expect(headings.indexOf("Configuration")).toBeGreaterThanOrEqual(0);
		expect(headings.indexOf("Schedule")).toBeGreaterThanOrEqual(0);
		expect(headings.indexOf("Configuration")).toBeLessThan(headings.indexOf("Schedule"));
	});

	// owner feedback: same section-heading style as the Admin tab's "Access"
	// heading (AgentAccessEditor.vue) - text-base font-medium text-ink-gray-9,
	// on both Configure columns, not just one.
	it("both column headings match the Admin tab's Access heading style", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		const grid = w.find(".grid.grid-cols-1.gap-10.lg\\:grid-cols-2.lg\\:items-start");
		const headings = grid.findAll(".text-base.font-medium.text-ink-gray-9");
		expect(headings.map((h) => h.text())).toEqual(["Configuration", "Schedule"]);
	});

	// jarvis#1062 owner decision: Comments moved off Configure onto the Run
	// itself (FindingsPanel.vue's new Notes section) - no CommentsSection
	// import remains here, so an accidental re-import would fail this mount
	// outright rather than silently resolving to a stub. Also: no orphaned
	// divider/empty space is left where Comments used to sit.
	it("no longer renders Comments on the Configure tab, and left no orphaned divider", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		expect(w.findComponent({ name: "CommentsSection" }).exists()).toBe(false);
		expect(w.text()).not.toContain("Comments");
		const grid = w.find(".grid.grid-cols-1.gap-10.lg\\:grid-cols-2.lg\\:items-start");
		expect(grid.find(".border-t").exists()).toBe(false);
	});
});

// owner feedback: "Save schedule" is not always-visible chrome, and the old
// bare "Next run: ..." line becomes a one-line summary of the SAVED
// schedule ("Scheduled monthly at 9:00 am. Next run: ...").
describe("Configure tab: Schedule section - Save visibility, dirty tracking, saved-schedule summary", () => {
	function scheduleSaveBtn(w) {
		return w.findAll("button").find((b) => b.attributes("data-label") === "Save schedule");
	}
	function runAutomaticallyToggle(w) {
		return w.findAll("button").find((b) => b.text() === "Run automatically");
	}

	it("Save is hidden when the form matches the saved installation (clean)", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 1,
					schedule_frequency: "daily",
					schedule_time: "09:00:00",
					next_run_at: "2026-09-10 09:00:00",
				}),
			})
		);
		expect(scheduleSaveBtn(w)).toBeFalsy();
	});

	it("Save appears once a control is edited (dirty) - toggling the switch", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1, schedule_enabled: 0 }) })
		);
		expect(scheduleSaveBtn(w)).toBeFalsy();
		await runAutomaticallyToggle(w).trigger("click");
		expect(scheduleSaveBtn(w)).toBeTruthy();
	});

	it("Save appears when Frequency/Time change while already on", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 1,
					schedule_frequency: "daily",
					schedule_time: "09:00:00",
					next_run_at: "2026-09-10 09:00:00",
				}),
			})
		);
		expect(scheduleSaveBtn(w)).toBeFalsy();
		const freqSelect = w.find("select");
		await freqSelect.setValue("weekly");
		expect(scheduleSaveBtn(w)).toBeTruthy();
	});

	it("Save disappears again once a save lands (installation refresh clears dirty)", async () => {
		routeMock.hash = "#configure";
		apiAgents.getAgent
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({ enabled: 1, schedule_enabled: 0 }),
				})
			)
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({
						enabled: 1,
						schedule_enabled: 1,
						schedule_frequency: "daily",
						schedule_time: "09:00:00",
						next_run_at: "2026-09-10 09:00:00",
					}),
				})
			);
		apiAgents.getInstallationActivation.mockResolvedValue(null);
		api.setAgentSchedule.mockResolvedValue({
			ok: true,
			data: { name: "INST-1", next_run_at: "2026-09-10 09:00:00" },
		});
		const w = mount(AgentDetail, { props: { slug: "close-auditor" } });
		await flushPromises();
		await flushPromises();

		await runAutomaticallyToggle(w).trigger("click");
		const btn = scheduleSaveBtn(w);
		expect(btn).toBeTruthy();
		await btn.trigger("click");
		await flushPromises();
		await flushPromises();

		expect(scheduleSaveBtn(w)).toBeFalsy();
	});

	it("off and saved (clean): only the toggle shows - no Frequency/Time, no Save", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 0,
					next_run_at: null,
				}),
			})
		);
		expect(runAutomaticallyToggle(w)).toBeTruthy();
		expect(w.find("select").exists()).toBe(false);
		expect(w.findComponent({ name: "TimePicker" }).exists()).toBe(false);
		expect(scheduleSaveBtn(w)).toBeFalsy();
	});

	it("off and saved, but with a stale next_run_at (legacy row): no summary either - saved schedule is off", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 0,
					next_run_at: "2026-09-10 09:00:00",
				}),
			})
		);
		expect(w.text()).not.toContain("Scheduled");
		expect(scheduleSaveBtn(w)).toBeFalsy();
	});

	it("a time-only edit round-trips through timeHHMM normalization - Save hides after save, not stuck on seconds", async () => {
		// TimePicker's modelValue can carry seconds ("10:30:00"); savedSched is
		// normalized ("HH:MM") - scheduleDirty must normalize both sides or
		// Save never hides after a legitimate time-only save.
		routeMock.hash = "#configure";
		apiAgents.getAgent
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({
						enabled: 1,
						schedule_enabled: 1,
						schedule_frequency: "daily",
						schedule_time: "09:00:00",
						next_run_at: "2026-09-10 09:00:00",
					}),
				})
			)
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({
						enabled: 1,
						schedule_enabled: 1,
						schedule_frequency: "daily",
						schedule_time: "10:30:00",
						next_run_at: "2026-09-10 10:30:00",
					}),
				})
			);
		apiAgents.getInstallationActivation.mockResolvedValue(null);
		api.setAgentSchedule.mockResolvedValue({
			ok: true,
			data: { name: "INST-1", next_run_at: "2026-09-10 10:30:00" },
		});
		const w = mount(AgentDetail, { props: { slug: "close-auditor" } });
		await flushPromises();
		await flushPromises();
		expect(scheduleSaveBtn(w)).toBeFalsy();

		await w.find('[data-testid="time-picker"]').trigger("click"); // emits "10:30:00"
		expect(scheduleSaveBtn(w)).toBeTruthy();

		await scheduleSaveBtn(w).trigger("click");
		await flushPromises();
		await flushPromises();

		expect(scheduleSaveBtn(w)).toBeFalsy();
	});

	it("saved and enabled (clean): shows the one-line summary in the muted style", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 1,
					schedule_frequency: "monthly",
					schedule_time: "09:00:00",
					next_run_at: "2026-10-03 09:00:00",
				}),
			})
		);
		const summary = w
			.findAll("div.text-sm.text-ink-gray-5")
			.find((d) => d.text().startsWith("Scheduled"));
		expect(summary).toBeTruthy();
		expect(summary.text()).toContain("Scheduled monthly at 9:00 am.");
		expect(summary.text()).toContain("Next run:");
	});

	it("editing a control while a summary is showing hides the summary, not just shows Save", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 1,
					schedule_frequency: "monthly",
					schedule_time: "09:00:00",
					next_run_at: "2026-10-03 09:00:00",
				}),
			})
		);
		expect(w.text()).toContain("Scheduled monthly");
		await w.find("select").setValue("weekly");
		expect(w.text()).not.toContain("Scheduled monthly");
		expect(scheduleSaveBtn(w)).toBeTruthy();
	});
});

describe("Configure tab: Next run line follows next_run_at, including after disabling the schedule", () => {
	it("shows the Next run line when next_run_at is set", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({
					enabled: 1,
					schedule_enabled: 1,
					schedule_frequency: "daily",
					next_run_at: "2026-09-10 09:00:00",
				}),
			})
		);
		expect(w.text()).toContain("Next run:");
	});

	it("hides the Next run line when next_run_at is null (schedule off)", async () => {
		routeMock.hash = "#configure";
		const w = await mountDetail(
			baseAgent({
				installation: installedInstallation({ enabled: 1, next_run_at: null }),
			})
		);
		expect(w.text()).not.toContain("Next run:");
	});

	it("re-fetching after a disable clears the line (agents#1062 next_run_at fix)", async () => {
		// jarvis chat/agents_api.py's set_schedule now nulls next_run_at when the
		// schedule is turned off; AgentDetail's saveSchedule() reloads via load(),
		// so the next getAgent() response deciding this is the contract that matters
		// here, not a client-side mutation of the previous response.
		routeMock.hash = "#configure";
		apiAgents.getAgent
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({
						enabled: 1,
						schedule_enabled: 1,
						schedule_frequency: "daily",
						next_run_at: "2026-09-10 09:00:00",
					}),
				})
			)
			.mockResolvedValueOnce(
				baseAgent({
					installation: installedInstallation({
						enabled: 1,
						schedule_enabled: 0,
						next_run_at: null,
					}),
				})
			);
		apiAgents.getInstallationActivation.mockResolvedValue(null);
		const w = mount(AgentDetail, { props: { slug: "close-auditor" } });
		await flushPromises();
		await flushPromises();
		expect(w.text()).toContain("Next run:");

		api.setAgentSchedule.mockResolvedValue({
			ok: true,
			data: { name: "INST-1", next_run_at: null },
		});
		// Save is hidden until the form is actually dirty (owner feedback) -
		// flip the switch off first, which is what makes it appear.
		const toggle = w.findAll("button").find((b) => b.text() === "Run automatically");
		await toggle.trigger("click");
		const saveScheduleBtn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Save schedule");
		expect(saveScheduleBtn).toBeTruthy();
		await saveScheduleBtn.trigger("click");
		await flushPromises();
		await flushPromises();

		expect(w.text()).not.toContain("Next run:");
	});
});

describe("jarvis#1062 fix: a #runs deep link survives the pending agent/installation fetch", () => {
	it("lands on Runs (not Overview) once installation resolves, from a hash requested before load", async () => {
		routeMock.hash = "#runs";
		routeMock.query = { run: "RUN-9" };
		let resolveFetch;
		apiAgents.getAgent.mockReturnValue(
			new Promise((res) => {
				resolveFetch = res;
			})
		);
		apiAgents.getInstallationActivation.mockResolvedValue(null);

		const w = mount(AgentDetail, { props: { slug: "close-auditor" } });
		await flushPromises();
		// Still loading - AgentRunsBoard cannot exist yet (no installation to
		// gate it on), but the hash must not have been thrown away either.
		expect(w.text()).toContain("Loading agent");
		expect(w.findComponent({ name: "AgentRunsBoard" }).exists()).toBe(false);
		expect(routeMock.query.run).toBe("RUN-9");

		resolveFetch(baseAgent({ installation: installedInstallation({ enabled: 1 }) }));
		await flushPromises();
		await flushPromises();

		expect(w.findComponent({ name: "AgentRunsBoard" }).exists()).toBe(true);
		// AgentDetail's own job stops at landing on the right tab - it must not
		// have consumed/cleared the run query itself; that is AgentRunsBoard's
		// job (see AgentRunsBoard.spec.js), and it needs the query intact when
		// it mounts.
		expect(routeMock.query.run).toBe("RUN-9");
	});

	it("a non-run hash (#overview) is unaffected - resolves immediately, no wait needed", async () => {
		routeMock.hash = "#overview";
		routeMock.query = {};
		let resolveFetch;
		apiAgents.getAgent.mockReturnValue(
			new Promise((res) => {
				resolveFetch = res;
			})
		);
		apiAgents.getInstallationActivation.mockResolvedValue(null);

		const w = mount(AgentDetail, { props: { slug: "close-auditor" } });
		await flushPromises();
		expect(w.findComponent({ name: "AgentRunsBoard" }).exists()).toBe(false);

		resolveFetch(baseAgent({ installation: installedInstallation({ enabled: 1 }) }));
		await flushPromises();
		await flushPromises();

		expect(w.findComponent({ name: "AgentRunsBoard" }).exists()).toBe(false);
		expect(w.findComponent({ name: "ConfigForm" }).exists()).toBe(false);
	});

	it("an eventually-invalid hash still falls back to Overview once the load truly settles", async () => {
		routeMock.hash = "#nonexistent-tab";
		routeMock.query = {};
		const w = await mountDetail(
			baseAgent({ installation: installedInstallation({ enabled: 1 }) })
		);
		// "nonexistent-tab" was never going to exist - overview wins once the
		// load has settled, same as before this fix.
		expect(w.findComponent({ name: "AgentRunsBoard" }).exists()).toBe(false);
	});
});

// jarvis#1062 P1-6 (production-readiness audit): the breadcrumb used to
// flash the raw slug before the agent loaded.
describe("Breadcrumb: no slug flash while the agent is loading (jarvis#1062 P1-6)", () => {
	it("shows an empty, loading crumb (skeleton) before the agent resolves - never the raw slug", async () => {
		let resolveFetch;
		apiAgents.getAgent.mockReturnValue(
			new Promise((res) => {
				resolveFetch = res;
			})
		);
		apiAgents.getInstallationActivation.mockResolvedValue(null);
		const w = mount(AgentDetail, { props: { slug: "negative-stock-valuation-auditor" } });
		await flushPromises();

		const crumbs = w.findAll(".crumb");
		const lastCrumb = crumbs[crumbs.length - 1];
		expect(lastCrumb.text()).not.toContain("negative-stock-valuation-auditor");
		expect(lastCrumb.attributes("data-loading")).toBe("true");

		resolveFetch(baseAgent({ agent_slug: "negative-stock-valuation-auditor" }));
		await flushPromises();
	});

	it("shows the real title, loading marker gone, once the agent resolves", async () => {
		const w = await mountDetail(baseAgent({ title: "Negative-Stock & Valuation Auditor" }));
		const crumbs = w.findAll(".crumb");
		const lastCrumb = crumbs[crumbs.length - 1];
		expect(lastCrumb.text()).toContain("Negative-Stock & Valuation Auditor");
		expect(lastCrumb.attributes("data-loading")).toBe("false");
	});
});
