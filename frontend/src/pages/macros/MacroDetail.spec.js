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
		props: ["modelValue", "label", "disabled"],
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
