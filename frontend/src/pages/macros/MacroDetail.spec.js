import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#653 regression coverage: the Frequency-change watcher that auto-fills
 * a default Day ("Monday" / "1") must NOT fire off seed()'s own frequency+day
 * assignment - opening an existing weekly/monthly macro with no anchor saved
 * (schedule_weekday / schedule_day_of_month both null) must load CLEAN (no
 * Save enabled), not silently dirty the page with a fabricated day nobody
 * chose. And the schedule summary must be built from the SAVED snapshot only,
 * blank while the form is dirty - never narrating an uncommitted draft against
 * a stale next_run_at.
 */

const api = vi.hoisted(() => ({
	getMacro: vi.fn(),
	createMacro: vi.fn(),
	updateMacro: vi.fn(),
	runMacro: vi.fn(),
	deleteMacro: vi.fn(),
	summarizeMacro: vi.fn(),
}));
vi.mock("@/api", () => api);

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("vue-router", () => ({
	useRouter: () => router,
	onBeforeRouteLeave: vi.fn(),
}));

vi.mock("frappe-ui", () => ({
	dayjsLocal: (d) => ({
		format: () => String(d || ""),
		fromNow: () => "",
		isValid: () => !!d,
		valueOf: () => (d ? new Date(String(d).replace(" ", "T")).getTime() : 0),
	}),
	toast: {
		success: vi.fn(),
		error: vi.fn(),
		create: vi.fn(),
	},
	confirmDialog: vi.fn(),
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "iconLeft", "tooltip", "icon"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span>{{ label }}</span>`,
	},
	Dropdown: { name: "Dropdown", props: ["options"], template: "<div><slot /></div>" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"></select>`,
	},
	Switch: {
		name: "Switch",
		props: ["modelValue", "label", "disabled", "description"],
		emits: ["update:modelValue"],
		template: `<button :disabled="disabled" @click="$emit('update:modelValue', !modelValue)">{{ label }}</button>`,
	},
	TimePicker: {
		name: "TimePicker",
		props: ["modelValue", "placeholder"],
		emits: ["update:modelValue"],
		template: `<button data-testid="time-picker" @click="$emit('update:modelValue', '10:30:00')">{{ modelValue }}</button>`,
	},
}));

vi.mock("@/components/doc/DocPage.vue", () => ({
	default: {
		name: "DocPage",
		props: ["breadcrumbs", "title", "statusBadge", "dirty", "loading", "error"],
		template: `<div><slot name="actions" /><slot name="main" /><slot name="aside" /><slot name="footer" /></div>`,
	},
}));
vi.mock("@/components/doc/DocSection.vue", () => ({
	default: {
		name: "DocSection",
		props: ["label", "opened"],
		template: `<div><slot /><slot name="header-suffix" /></div>`,
	},
}));
vi.mock("@/components/doc/DocMetaPanel.vue", () => ({
	default: { name: "DocMetaPanel", template: "<div />" },
}));
vi.mock("@/components/doc/CommentsSection.vue", () => ({
	default: { name: "CommentsSection", template: "<div />" },
}));
vi.mock("@/pages/macros/StepsBuilder.vue", () => ({
	default: { name: "StepsBuilder", template: "<div />" },
}));
vi.mock("@/composables/useDocmeta", () => ({ useDocmeta: () => ({}) }));
vi.mock("@/composables/macroPrefill", () => ({ takeMacroPrefill: () => null }));
vi.mock("@/branding", () => ({ agentName: "Jarvis" }));
// A named user is logged in throughout: the schedule switch for a SAVED macro must
// follow what the server says about the macro's owner, not who is looking.
const session = vi.hoisted(() => ({ user: "priya@example.com" }));
vi.mock("@/data/session", () => ({ session }));
vi.mock("@/lib/errors", () => ({
	errMessage: (e) => (e && e.message) || String(e),
	errHtml: (e) => (e && e.message) || String(e),
}));

import MacroDetail from "./MacroDetail.vue";

function baseMacro(overrides = {}) {
	return {
		name: "MACRO-1",
		macro_name: "Month-end close",
		description: "",
		enabled: 1,
		stop_on_error: 1,
		skip_confirmation: 0,
		schedule_enabled: 1,
		schedule_frequency: "daily",
		schedule_weekday: null,
		schedule_day_of_month: null,
		schedule_time: "09:00:00",
		next_run_at: null,
		merged_prompt: "",
		merge_status: "",
		steps: [
			{
				label: "",
				prompt: "do the thing",
				model_override: "",
				thinking_override: "",
				skills: [],
			},
		],
		...overrides,
	};
}

async function mountDetail(macroFixture) {
	api.getMacro.mockResolvedValue(macroFixture);
	const w = mount(MacroDetail, {
		props: { id: macroFixture.name, isNew: false },
		global: { provide: { $socket: null } },
	});
	await flushPromises();
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	session.user = "priya@example.com";
});

function saveBtn(w) {
	return w.findAll("button").find((b) => b.attributes("data-label") === "Save");
}

describe("MacroDetail Schedule section: seeding must not fabricate a day", () => {
	it("loading a weekly macro with no saved weekday loads clean (no Save)", async () => {
		const w = await mountDetail(
			baseMacro({ schedule_frequency: "weekly", schedule_weekday: null, next_run_at: null })
		);
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("loading a monthly macro with no saved day-of-month loads clean (no Save)", async () => {
		const w = await mountDetail(
			baseMacro({
				schedule_frequency: "monthly",
				schedule_day_of_month: null,
				next_run_at: null,
			})
		);
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("loading a weekly macro WITH a saved weekday loads clean and keeps it", async () => {
		const w = await mountDetail(
			baseMacro({
				schedule_frequency: "weekly",
				schedule_weekday: "Wednesday",
				next_run_at: null,
			})
		);
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("an interactive Frequency change to weekly DOES default the day (and dirties the form)", async () => {
		const w = await mountDetail(baseMacro({ schedule_frequency: "daily", next_run_at: null }));
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
		// The FormControl stub renders a plain <select> with NO <option>s, so a
		// native setValue("weekly") can never actually take (jsdom keeps a
		// <select>'s .value at "" with no matching option to select) - drive
		// the update the way the real component would receive it, straight
		// through the component's own update:modelValue emit.
		const freqControl = w
			.findAllComponents({ name: "FormControl" })
			.find((c) => c.attributes("label") === "Frequency");
		expect(freqControl).toBeTruthy();
		await freqControl.vm.$emit("update:modelValue", "weekly");
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
		// And the Day control picked up the fallback default, not a phantom
		// empty selection.
		const dayControl = w
			.findAllComponents({ name: "FormControl" })
			.find((c) => c.attributes("label") === "Day");
		expect(dayControl.props("modelValue")).toBe("Monday");
	});
});

describe("MacroDetail Schedule section: a pending retry is not shown as the schedule", () => {
	// A failed scheduled run is retried at a time the owner never chose. Shown as a
	// plain "Next run" it contradicts the schedule printed right before it.
	const retrying = { next_run_at: "2026-10-01 11:17:04", next_run_is_retry: 1 };

	it("says the last run failed, where to look, and when it retries", async () => {
		const w = await mountDetail(baseMacro(retrying));
		expect(w.text()).toContain("Scheduled daily at 9:00 am.");
		expect(w.text()).toContain(
			"The last scheduled run failed (Runs, on the Macros page, says why). Retrying:"
		);
		expect(w.text()).not.toContain("Next run:");
	});

	it("an ordinary slot, or a server that sends no flag, still reads 'Next run'", async () => {
		const w = await mountDetail(baseMacro({ next_run_at: "2026-10-02 09:00:00" }));
		expect(w.text()).toContain("Next run:");
		expect(w.text()).not.toContain("Retrying:");
	});
});

describe("MacroDetail: how the last run went", () => {
	// Nothing on the form said whether the macro worked the last time it ran.
	it("a failed last run is stated with its reason and a way to open it", async () => {
		const w = await mountDetail(
			baseMacro({
				last_run: {
					status: "failed",
					error: "Step 2 failed: no such customer",
					conversation: "conv-run",
				},
			})
		);
		expect(w.text()).toContain("The last run failed. Step 2 failed: no such customer");
		expect(w.html()).toContain("/c/conv-run");
	});

	it("a run waiting on a confirmation says what it is waiting for", async () => {
		const reason = "Step 1 is waiting for your confirmation (Send email).";
		const w = await mountDetail(baseMacro({ last_run: { status: "stopped", error: reason } }));
		expect(w.text()).toContain(reason);
		expect(w.text()).not.toContain("Open the run");
	});

	it("a last run that simply worked adds nothing to the form", async () => {
		const w = await mountDetail(baseMacro({ last_run: { status: "completed", error: "" } }));
		expect(w.find('[role="status"]').exists()).toBe(false);
	});

	it("a macro that never ran, or a server that sends no last run, adds nothing", async () => {
		expect(
			(await mountDetail(baseMacro({ last_run: null }))).find('[role="status"]').exists()
		).toBe(false);
		expect((await mountDetail(baseMacro())).find('[role="status"]').exists()).toBe(false);
	});
});

describe("MacroDetail Schedule section: summary line reflects the SAVED snapshot only", () => {
	it("shows the summary when clean and a next_run_at is present", async () => {
		const w = await mountDetail(
			baseMacro({
				schedule_frequency: "monthly",
				schedule_day_of_month: 15,
				next_run_at: "2026-10-15 09:00:00",
			})
		);
		expect(w.text()).toContain("Scheduled monthly on the 15th at 9:00 am.");
		expect(w.text()).toContain("Next run:");
	});

	it("blanks the summary while the schedule time itself is being edited (unsaved)", async () => {
		const w = await mountDetail(
			baseMacro({
				schedule_frequency: "monthly",
				schedule_day_of_month: 15,
				next_run_at: "2026-10-15 09:00:00",
			})
		);
		expect(w.text()).toContain("Scheduled monthly");
		// dirty the schedule time (unsaved) - the summary must not narrate the
		// draft against the SAVED next_run_at, which never recomputes on its own.
		await w.find('[data-testid="time-picker"]').trigger("click");
		expect(w.text()).not.toContain("Scheduled monthly on the 15th at 9:00 am.");
	});

	it("blanks the summary even when an UNRELATED field is dirtied (macro_name)", async () => {
		// jarvis#653 defect 2: the summary must be gated on the whole-form dirty
		// flag, not a schedule-only one - MacroDetail has a single Save button
		// for the whole page, so an edit anywhere leaves the page in a draft
		// state the summary must not narrate as committed.
		const w = await mountDetail(
			baseMacro({
				schedule_frequency: "monthly",
				schedule_day_of_month: 15,
				next_run_at: "2026-10-15 09:00:00",
			})
		);
		expect(w.text()).toContain("Scheduled monthly");
		// The FormControl stub has no <option>s, so drive the change through
		// the component's own emit (see the Frequency test above for why a
		// native setValue on this stub cannot be trusted).
		const nameControl = w
			.findAllComponents({ name: "FormControl" })
			.find((c) => c.attributes("label") === "Name");
		expect(nameControl).toBeTruthy();
		await nameControl.vm.$emit("update:modelValue", "Renamed macro");
		expect(w.text()).not.toContain("Scheduled monthly on the 15th at 9:00 am.");
	});
});

describe("MacroDetail Schedule switch: an owner the scheduler will never run", () => {
	const REASON =
		"Scheduled macros cannot run as Administrator. Sign in as a named user to schedule a macro, or switch the schedule off to save this one.";

	function scheduleSwitch(w) {
		return w
			.findAllComponents({ name: "Switch" })
			.find((c) => c.props("label") === "Run on a schedule");
	}

	it("says why and cannot be switched ON while the schedule is off", async () => {
		const w = await mountDetail(
			baseMacro({ schedule_enabled: 0, schedule_blocked_reason: REASON })
		);
		const sw = scheduleSwitch(w);
		expect(sw.props("description")).toBe(REASON);
		expect(sw.props("disabled")).toBe(true);
	});

	it("says why but can still be switched OFF when the schedule is already on", async () => {
		// Every save of such a macro is refused until the schedule is off, so the one
		// control that gets the owner unstuck must stay usable.
		const w = await mountDetail(
			baseMacro({ schedule_enabled: 1, schedule_blocked_reason: REASON })
		);
		const sw = scheduleSwitch(w);
		expect(sw.props("description")).toBe(REASON);
		expect(sw.props("disabled")).toBe(false);
	});

	it("a NEW macro asks about the logged-in account, since it has no owner yet", async () => {
		session.user = "Administrator";
		const w = mount(MacroDetail, {
			props: { id: "", isNew: true },
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		const sw = scheduleSwitch(w);
		expect(sw.props("description")).toContain("Administrator");
		expect(sw.props("disabled")).toBe(true);
	});

	it("a saved macro ignores who is logged in and follows the server", async () => {
		// Administrator looking at a named user's macro: the server reports no block,
		// so the logged-in account must not put one there.
		session.user = "Administrator";
		const w = await mountDetail(
			baseMacro({ schedule_enabled: 0, schedule_blocked_reason: "" })
		);
		expect(scheduleSwitch(w).props("disabled")).toBe(false);
	});

	it("is an ordinary switch when the server reports no block", async () => {
		const w = await mountDetail(
			baseMacro({ schedule_enabled: 0, schedule_blocked_reason: "" })
		);
		const sw = scheduleSwitch(w);
		expect(sw.props("description")).toBe("Jarvis runs this macro automatically.");
		expect(sw.props("disabled")).toBe(false);
	});
});
