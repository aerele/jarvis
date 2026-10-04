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

// The site timezone the shell was told (AppShell's setConfig("systemTimezone")).
// Unset by default: that is the fallback every older test in this file runs under.
// `viewerClock`: what a server datetime reads as on the VIEWER's clock. Unset, the
// viewer is in the site's zone and a datetime reads as stored.
const siteConfig = vi.hoisted(() => ({ systemTimezone: "", viewerClock: "" }));
vi.mock("frappe-ui", () => ({
	getConfig: (key) => siteConfig[key],
	dayjs: () => ({ format: () => "" }),
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
	dayjsLocal: (d) => ({
		format: () => siteConfig.viewerClock || String(d || ""),
		fromNow: () => "",
		isValid: () => !!d,
		valueOf: () => (d ? new Date(String(d).replace(" ", "T")).getTime() : 0),
	}),
	toast: {
		success: vi.fn(),
		error: vi.fn(),
		create: vi.fn(),
		remove: vi.fn(),
	},
	confirmDialog: vi.fn(),
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<i />" },
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
	default: {
		name: "StepsBuilder",
		props: ["modelValue", "disabled", "errors"],
		emits: ["update:modelValue"],
		template: "<div />",
	},
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
	// The real rule, not a pass-through: the delete confirmation is an HTML sink
	// and the test below reads what reaches it.
	escapeHtml: (v) =>
		String(v ?? "").replace(
			/[&<>"']/g,
			(c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
		),
}));

import { toast, confirmDialog } from "frappe-ui";
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
	siteConfig.systemTimezone = "";
	siteConfig.viewerClock = "";
});

function saveBtn(w) {
	return w.findAll("button").find((b) => b.attributes("data-label") === "Save");
}
function runBtn(w) {
	return w.findAllComponents({ name: "Button" }).find((b) => b.props("label") === "Run");
}
// The "..." button that opens the menu (Re-summarize, Delete).
function menuBtn(w) {
	return w
		.findAllComponents({ name: "Button" })
		.find((b) => b.props("icon") === "more-horizontal");
}
// The visible line that says why Run is off (the button's accessible description).
function runReason(w) {
	return w.find('[data-testid="run-reason"]');
}
function nameField(w) {
	return w
		.findAllComponents({ name: "FormControl" })
		.find((c) => c.attributes("label") === "Name");
}
function stepsBuilder(w) {
	return w.findComponent({ name: "StepsBuilder" });
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
	const note = (w) => w.find('[data-testid="last-run-note"]');
	const openChat = (w) =>
		w.findAll("button").find((b) => b.attributes("data-label") === "Open the chat");
	const nameControl = (w) =>
		w.findAllComponents({ name: "FormControl" }).find((c) => c.attributes("label") === "Name");
	const withSocket = async (macro) => {
		const handlers = [];
		const socket = { on: (_e, fn) => handlers.push(fn), off: () => {} };
		api.getMacro.mockResolvedValue(macro);
		const w = mount(MacroDetail, {
			props: { id: "MACRO-1", isNew: false },
			global: { provide: { $socket: socket } },
		});
		await flushPromises();
		return { w, emit: (p) => handlers.forEach((fn) => fn(p)) };
	};

	it("a failed last run is stated with its reason and a way to open it", async () => {
		const w = await mountDetail(
			baseMacro({
				last_run: {
					status: "failed",
					error: "Step 2 failed: The model is overloaded.",
					conversation: "conv-run",
				},
			})
		);
		expect(note(w).text()).toContain("The last run failed");
		expect(note(w).text()).toContain("Step 2 failed: The model is overloaded.");
		await openChat(w).trigger("click");
		expect(router.push).toHaveBeenCalledWith("/c/conv-run");
	});

	it("a run stopped at a confirmation says so, and offers no chat it cannot open", async () => {
		const reason =
			"Stopped at step 1 of 2: a confirmation is waiting in the conversation (Send email).";
		const w = await mountDetail(baseMacro({ last_run: { status: "stopped", error: reason } }));
		expect(note(w).text()).toContain("The last run stopped before it finished");
		expect(note(w).text()).toContain(reason);
		expect(openChat(w)).toBeUndefined();
	});

	it("a last run that simply worked adds nothing to the form", async () => {
		const w = await mountDetail(baseMacro({ last_run: { status: "completed", error: "" } }));
		expect(note(w).exists()).toBe(false);
	});

	it("a macro that never ran, or a server that sends no last run, adds nothing", async () => {
		expect(note(await mountDetail(baseMacro({ last_run: null }))).exists()).toBe(false);
		expect(note(await mountDetail(baseMacro())).exists()).toBe(false);
	});

	it("is refreshed when a run of this macro ends, without touching the form", async () => {
		// The owner clicks Run and watches it fail: the form must not still say the
		// previous run was fine. And it must not throw away what they were typing.
		const { w, emit } = await withSocket(baseMacro({ last_run: null }));
		expect(note(w).exists()).toBe(false);
		await nameControl(w).vm.$emit("update:modelValue", "Half-typed name");
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();

		api.getMacro.mockResolvedValue(
			baseMacro({
				macro_name: "Renamed on the server",
				last_run: { status: "failed", error: "Step 1 failed: The model is overloaded." },
			})
		);
		emit({ kind: "macro:done", macro: "SOMEONE-ELSE", status: "failed" });
		await flushPromises();
		expect(note(w).exists()).toBe(false);

		emit({ kind: "macro:done", macro: "MACRO-1", status: "failed" });
		await flushPromises();
		expect(note(w).text()).toContain("Step 1 failed: The model is overloaded.");
		// Only the last-run notice changed: the unsaved edit is still in the field and
		// still waiting to be saved.
		expect(nameControl(w).props("modelValue")).toBe("Half-typed name");
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
	});

	it("a run that starts while the form is open is learned of, once", async () => {
		// The delete confirmation warns about a live run only if the page knows of
		// one. A run started by the scheduler, or from another tab, sends progress
		// events: the first is enough, the later steps of the same run are not asked.
		const { emit } = await withSocket(baseMacro({ last_run: null }));
		api.getMacro.mockClear();
		api.getMacro.mockResolvedValue(baseMacro({ last_run: { status: "running", error: "" } }));
		emit({ kind: "macro:progress", macro: "SOMEONE-ELSE", step: 1 });
		await flushPromises();
		expect(api.getMacro).not.toHaveBeenCalled();
		emit({ kind: "macro:progress", macro: "MACRO-1", step: 1 });
		await flushPromises();
		expect(api.getMacro).toHaveBeenCalledTimes(1);
		emit({ kind: "macro:progress", macro: "MACRO-1", step: 2 });
		await flushPromises();
		expect(api.getMacro).toHaveBeenCalledTimes(1);
	});

	it("a refresh that lands after the user opened another macro is dropped", async () => {
		const { w, emit } = await withSocket(baseMacro({ last_run: null }));
		let landLate;
		api.getMacro.mockImplementation(
			(id) =>
				new Promise((resolve) => {
					if (id === "MACRO-1")
						landLate = () =>
							resolve(
								baseMacro({
									last_run: { status: "failed", error: "Macro one failed." },
								})
							);
					else resolve(baseMacro({ name: "MACRO-2", last_run: null }));
				})
		);
		emit({ kind: "macro:done", macro: "MACRO-1", status: "failed" });
		await w.setProps({ id: "MACRO-2" });
		await flushPromises();
		landLate();
		await flushPromises();
		expect(note(w).exists()).toBe(false);
	});
});

describe("MacroDetail Delete: a macro that is running", () => {
	// Deleting used to remove the run row from under a run that kept going. The
	// server now stops the run first; the dialog and the toast say so.
	const openDelete = async (macro) => {
		const w = await mountDetail(macro);
		const overflow = w
			.findAllComponents({ name: "Dropdown" })
			.map((d) => d.props("options") || [])
			.flat()
			.find((o) => o.label === "Delete");
		overflow.onClick();
		return confirmDialog.mock.calls.at(-1)[0];
	};

	it("the confirmation says the run will be stopped", async () => {
		for (const status of ["running", "waiting_capacity"]) {
			const dialog = await openDelete(baseMacro({ last_run: { status, error: "" } }));
			expect(dialog.message).toContain("This macro is running. Deleting it stops the run.");
			expect(dialog.message).toContain("Its run history is deleted too.");
		}
	});

	it("a macro that is not running gets the plain confirmation", async () => {
		for (const last_run of [null, undefined, { status: "completed", error: "" }]) {
			const dialog = await openDelete(baseMacro({ last_run }));
			expect(dialog.message).not.toContain("running");
			expect(dialog.message).toContain("Its run history is deleted too.");
		}
	});

	it("the toast says a run was stopped when the server stopped one", async () => {
		const dialog = await openDelete(baseMacro({ last_run: { status: "running", error: "" } }));
		api.deleteMacro.mockResolvedValue({ ok: true, stopped_runs: 1 });
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(api.deleteMacro).toHaveBeenCalledWith("MACRO-1");
		expect(toast.success).toHaveBeenCalledWith("Macro deleted. 1 run was stopped.");
	});

	it("the toast stays as it was when nothing was running, or an older server answers", async () => {
		for (const answer of [{ ok: true, stopped_runs: 0 }, { ok: true }, undefined]) {
			const dialog = await openDelete(baseMacro());
			api.deleteMacro.mockResolvedValue(answer);
			await dialog.onConfirm({ hideDialog: vi.fn() });
			expect(toast.success).toHaveBeenLastCalledWith("Macro deleted");
		}
	});

	it("a second press of Confirm does not start a second delete", async () => {
		// frappe-ui's ConfirmDialog does not wait for onConfirm, so its button stays
		// live. A delete that stops a run takes seconds; the second request found the
		// macro gone and showed an error for a delete that had worked.
		const dialog = await openDelete(baseMacro({ last_run: { status: "running", error: "" } }));
		let land;
		api.deleteMacro.mockReturnValue(new Promise((resolve) => (land = resolve)));
		const hideDialog = vi.fn();
		const first = dialog.onConfirm({ hideDialog });
		// The dialog closes at once, not when the server answers.
		expect(hideDialog).toHaveBeenCalledTimes(1);
		await dialog.onConfirm({ hideDialog });
		expect(api.deleteMacro).toHaveBeenCalledTimes(1);
		land({ ok: true, stopped_runs: 1 });
		await first;
		expect(toast.success).toHaveBeenCalledTimes(1);
		expect(toast.error).not.toHaveBeenCalled();
	});

	it("a slow delete says what it is doing, and takes the note away when it is done", async () => {
		vi.useFakeTimers();
		try {
			const dialog = await openDelete(
				baseMacro({ last_run: { status: "running", error: "" } })
			);
			let land;
			api.deleteMacro.mockReturnValue(new Promise((resolve) => (land = resolve)));
			toast.create.mockReturnValue("note-1");
			const going = dialog.onConfirm({ hideDialog: vi.fn() });
			expect(toast.create).not.toHaveBeenCalled();
			vi.advanceTimersByTime(1000);
			expect(toast.create).toHaveBeenCalledWith(
				expect.objectContaining({ message: "Stopping the run and deleting the macro..." })
			);
			land({ ok: true, stopped_runs: 1 });
			await going;
			expect(toast.remove).toHaveBeenCalledWith("note-1");
		} finally {
			vi.useRealTimers();
		}
	});

	it("a refused delete shows the server's sentence, and Delete can be pressed again", async () => {
		const dialog = await openDelete(baseMacro());
		api.deleteMacro.mockRejectedValue(new Error("refused"));
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(toast.error).toHaveBeenCalledTimes(1);
		expect(toast.success).not.toHaveBeenCalled();
		api.deleteMacro.mockResolvedValue({ ok: true });
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(api.deleteMacro).toHaveBeenCalledTimes(2);
		expect(toast.success).toHaveBeenLastCalledWith("Macro deleted");
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

describe("MacroDetail Run: it runs what is SAVED, so it waits for a save", () => {
	it("is offered on a clean form, with nothing to explain", async () => {
		const w = await mountDetail(baseMacro());
		expect(runBtn(w).props("disabled")).toBe(false);
		expect(runBtn(w).props("tooltip")).toBe("Run this macro now");
		expect(runReason(w).exists()).toBe(false);
		expect(runBtn(w).attributes("aria-describedby")).toBeUndefined();
	});

	it("is off while the form has unsaved changes, and says why in text tied to the button", async () => {
		// The server runs the stored macro: with edits on screen, Run would start
		// something other than what the owner is looking at. The reason is a visible
		// line, not only a tooltip: a disabled button takes no focus and no hover on
		// touch, so a tooltip on it reaches neither a keyboard nor a screen reader.
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(runBtn(w).props("disabled")).toBe(true);
		expect(runReason(w).text()).toContain("Save your changes first");
		expect(runReason(w).attributes("id")).toBeTruthy();
		expect(runBtn(w).attributes("aria-describedby")).toBe(runReason(w).attributes("id"));
		await runBtn(w).trigger("click");
		expect(api.runMacro).not.toHaveBeenCalled();
	});

	it("refuses inside run() too, not only by disabling the button", async () => {
		// The stub's click is emitted straight to the handler, as a real click would
		// be if the button were ever left enabled.
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		runBtn(w).vm.$emit("click");
		await flushPromises();
		expect(api.runMacro).not.toHaveBeenCalled();
	});

	it("comes back once the edit is undone", async () => {
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		await nameField(w).vm.$emit("update:modelValue", "Month-end close");
		expect(runBtn(w).props("disabled")).toBe(false);
		expect(runReason(w).exists()).toBe(false);
	});

	it("keeps a line's room for the reason, so the form does not jump when it appears", async () => {
		// The line sits above the form. Without room kept for it, the first keystroke
		// pushed every field down, the one being typed in included.
		const w = await mountDetail(baseMacro());
		const room = w.find('[data-testid="run-reason-room"]');
		expect(room.exists()).toBe(true);
		expect(room.classes()).toContain("min-h-5");
		expect(room.text()).toBe("");
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(room.element.contains(runReason(w).element)).toBe(true);
	});

	it("still waits for a summary that is being written, and says that first, on the same line", async () => {
		const w = await mountDetail(baseMacro({ merge_status: "pending" }));
		expect(runBtn(w).props("disabled")).toBe(true);
		expect(runReason(w).text()).toContain("Summarizing");
		expect(runBtn(w).attributes("aria-describedby")).toBe(runReason(w).attributes("id"));
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(runReason(w).text()).toContain("Summarizing");
	});
});

describe("MacroDetail save: a save that worked leaves the form clean", () => {
	const edited = async () => {
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		return w;
	};

	it("even when the reload after it fails: Run is back, Save is off", async () => {
		// The snapshot used to be refreshed only by the reload. When that one request
		// failed the form stayed "dirty", so Run stayed off saying "Save your changes
		// first" about changes the server already had.
		api.updateMacro.mockResolvedValue({});
		const w = await edited();
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await saveBtn(w).trigger("click");
		await flushPromises();

		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(toast.success).toHaveBeenCalledWith("Saved “Renamed macro”");
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
		expect(runBtn(w).props("disabled")).toBe(false);
		expect(runReason(w).exists()).toBe(false);
		// What was saved is what stays on screen.
		expect(nameField(w).props("modelValue")).toBe("Renamed macro");
	});

	it("says, separately, that the page could not be refreshed", async () => {
		api.updateMacro.mockResolvedValue({});
		const w = await edited();
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(toast.error).not.toHaveBeenCalled();
		const notes = toast.create.mock.calls.map((c) => c[0]);
		expect(notes).toHaveLength(1);
		expect(notes[0].type).toBe("warning");
		expect(notes[0].message).toContain("Saved");
		expect(notes[0].message).toContain("Reload");
	});

	it("says nothing extra when the reload works", async () => {
		api.updateMacro.mockResolvedValue({});
		const w = await edited();
		api.getMacro.mockResolvedValue(baseMacro({ macro_name: "Renamed macro" }));
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(toast.create).not.toHaveBeenCalled();
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("a save that FAILED leaves the form dirty, as before", async () => {
		api.updateMacro.mockRejectedValue(new Error("Not permitted."));
		const w = await edited();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
		expect(runReason(w).text()).toContain("Save your changes first");
	});

	it("does not keep a summary the save made the server drop, when the reload fails", async () => {
		// Changed steps with an untouched summary: the save omits the summary and the
		// server clears its stale copy. Left on the form, the next rename-only save
		// would have sent the stale text back as if the owner had written it.
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		api.summarizeMacro.mockResolvedValue({});
		const w = await mountDetail(
			baseMacro({ merged_prompt: "Old summary of one step.", merge_status: "ready" })
		);
		const summary = () =>
			w
				.findAllComponents({ name: "FormControl" })
				.find((c) =>
					String(c.attributes("placeholder") || "").startsWith("No summary yet")
				);
		expect(summary().props("modelValue")).toBe("Old summary of one step.");
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "", prompt: "then this", skills: [] },
		]);
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect("merged_prompt" in api.updateMacro.mock.calls[0][0]).toBe(false);
		expect(summary().props("modelValue")).toBe("");
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
	});

	it("knows a summary was started even when the reload fails, so Run waits for it", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		api.summarizeMacro.mockResolvedValue({});
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "", prompt: "then this", skills: [] },
		]);
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.summarizeMacro).toHaveBeenCalledTimes(1);
		expect(runBtn(w).props("disabled")).toBe(true);
		expect(runReason(w).text()).toContain("Summarizing");
	});
});

describe("MacroDetail save: the server says whether to summarize", () => {
	// The form used to decide by itself: "the steps look changed, or there is no
	// summary". The second half started a model call on every save of a macro whose
	// summary was empty or had failed, and posting the summary back on a rename ended
	// a summary that was still being written.
	const TWO = [
		{
			label: "",
			prompt: "do the thing",
			model_override: "",
			thinking_override: "",
			skills: [],
		},
		{ label: "", prompt: "then this", model_override: "", thinking_override: "", skills: [] },
	];
	const summaryField = (w) =>
		w
			.findAllComponents({ name: "FormControl" })
			.find((c) => String(c.attributes("placeholder") || "").startsWith("No summary yet"));
	const save = async (w) => {
		await saveBtn(w).trigger("click");
		await flushPromises();
	};

	it("a rename starts nothing, on a macro the old rule summarized on every save", async () => {
		// Two steps and no summary.
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(baseMacro({ steps: TWO, merge_status: "failed" }));
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		await save(w);
		const sent = api.updateMacro.mock.calls[0][0];
		expect(sent.macro_name).toBe("Renamed macro");
		expect(sent.steps).toHaveLength(2);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
		expect(toast.create).not.toHaveBeenCalled();
	});

	it("a rename sends the summary it shows, and says the owner did not edit it", async () => {
		// Sent, because a server from before this change clears a summary that is
		// missing from a save. Marked as not edited, because what the form shows may
		// be old (the summary landed after the form loaded), and the server must not
		// take old text for the owner's.
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		await save(w);
		const sent = api.updateMacro.mock.calls[0][0];
		expect(sent.merged_prompt).toBe("Both, as one.");
		expect(sent.merged_prompt_edited).toBe(0);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
		expect(summaryField(w).props("modelValue")).toBe("Both, as one.");
	});

	it("changed steps with an untouched summary send no summary", async () => {
		// Both servers clear it for that (the old one because it is missing, the new
		// one because the steps changed).
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await stepsBuilder(w).vm.$emit("update:modelValue", [TWO[1], TWO[0]]);
		await save(w);
		const sent = api.updateMacro.mock.calls[0][0];
		expect("merged_prompt" in sent).toBe(false);
		expect(sent.merged_prompt_edited).toBe(0);
	});

	it("starts a summary when the server says so", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		api.summarizeMacro.mockResolvedValue({ ok: true, conversation: "c1" });
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await save(w);
		expect(api.summarizeMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-1");
	});

	it("starts none when the server says no, whatever the form thinks changed", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await save(w);
		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});

	it("sends a summary the owner edited, trimmed", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await summaryField(w).vm.$emit("update:modelValue", "  My own wording.  ");
		await save(w);
		expect(api.updateMacro.mock.calls[0][0].merged_prompt).toBe("My own wording.");
		expect(api.updateMacro.mock.calls[0][0].merged_prompt_edited).toBe(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});

	it("sends an edited summary together with changed steps", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await stepsBuilder(w).vm.$emit("update:modelValue", [TWO[1], TWO[0]]);
		await summaryField(w).vm.$emit("update:modelValue", "The other way round.");
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await save(w);
		expect(api.updateMacro.mock.calls[0][0].merged_prompt).toBe("The other way round.");
		expect(api.updateMacro.mock.calls[0][0].merged_prompt_edited).toBe(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
		// It was saved, so it stays on the form.
		expect(summaryField(w).props("modelValue")).toBe("The other way round.");
	});

	it("a new macro is summarized when the server says so", async () => {
		const mountNew = async () => {
			const w = mount(MacroDetail, {
				props: { id: "", isNew: true },
				global: { provide: { $socket: null } },
			});
			await flushPromises();
			await nameField(w).vm.$emit("update:modelValue", "Brand new");
			await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
			return w;
		};
		api.createMacro.mockResolvedValue({ data: { name: "MACRO-9", summarize: true } });
		api.summarizeMacro.mockResolvedValue({ ok: true });
		await save(await mountNew());
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-9");
		expect(router.replace).toHaveBeenCalledWith("/macros/MACRO-9");

		vi.clearAllMocks();
		api.createMacro.mockResolvedValue({ data: { name: "MACRO-10", summarize: false } });
		await save(await mountNew());
		expect(api.createMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});

	it("does not say Summarizing when the summary was not started", async () => {
		// The site was busy: the server answers ok false and nothing is pending.
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		api.summarizeMacro.mockResolvedValue({ ok: false, reason: "The site is busy." });
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		api.getMacro.mockRejectedValue(new Error("The server is not reachable."));
		await save(w);
		expect(toast.success).toHaveBeenCalledWith("Saved “Month-end close”");
		expect(runBtn(w).props("disabled")).toBe(false);
		// Two warnings and no "Summarizing": the summary, and the failed reload.
		expect(toast.create.mock.calls.map((c) => c[0].type)).toEqual(["warning", "warning"]);
	});

	it("Re-summarize in the menu still starts one", async () => {
		api.summarizeMacro.mockResolvedValue({ ok: true, conversation: "c1" });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		const action = w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Re-summarize");
		await action.onClick();
		await flushPromises();
		// `true`: the owner asked by name, so a summary already pending is replaced.
		// Without it the server answers "that one is pending", which left a stuck
		// "Summarizing" with no way out.
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-1", true);
		expect(runReason(w).text()).toContain("Summarizing");
	});

	it("Re-summarize says why when the summary was not started", async () => {
		api.summarizeMacro.mockResolvedValue({ ok: false, reason: "The site is busy." });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		const action = w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Re-summarize");
		await action.onClick();
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"The summary of “Month-end close” was not started: The site is busy."
		);
		expect(runBtn(w).props("disabled")).toBe(false);
	});

	it("Re-summarize that was refused names the macro too, and its name is text", async () => {
		api.summarizeMacro.mockRejectedValue(new Error("Connect an AI model first"));
		const w = await mountDetail(
			baseMacro({ macro_name: "<b>Close</b>", steps: TWO, merge_status: "ready" })
		);
		const action = w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Re-summarize");
		await action.onClick();
		await flushPromises();
		// The toast renders HTML. A reason that came without a full stop gets one.
		expect(toast.error).toHaveBeenCalledWith(
			"The summary of “&lt;b&gt;Close&lt;/b&gt;” was not started: Connect an AI model first."
		);
	});
});

describe("MacroDetail save: against a server from before `summarize`", () => {
	// Assets and Python do not switch at the same instant, and a tab keeps the bundle
	// it loaded. A server from before this change answers a save without `summarize`,
	// clears a summary that is missing from the save, and ignores the arguments it
	// does not know. Against it the form does what the form before it did.
	const TWO = [
		{
			label: "",
			prompt: "do the thing",
			model_override: "",
			thinking_override: "",
			skills: [],
		},
		{ label: "", prompt: "then this", model_override: "", thinking_override: "", skills: [] },
	];
	const OLD_SERVER = { data: { name: "MACRO-1", modified: "2026-10-03 10:00:00" } };
	const save = async (w) => {
		await saveBtn(w).trigger("click");
		await flushPromises();
	};

	it("a rename keeps the summary (it is sent) and starts nothing", async () => {
		api.updateMacro.mockResolvedValue(OLD_SERVER);
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		await save(w);
		expect(api.updateMacro.mock.calls[0][0].merged_prompt).toBe("Both, as one.");
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});

	it("changed steps start a summary, though the server did not ask for one", async () => {
		api.updateMacro.mockResolvedValue(OLD_SERVER);
		api.summarizeMacro.mockResolvedValue({ ok: true, conversation: "c1" });
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		await stepsBuilder(w).vm.$emit("update:modelValue", [TWO[1], TWO[0]]);
		await save(w);
		expect("merged_prompt" in api.updateMacro.mock.calls[0][0]).toBe(false);
		expect(api.summarizeMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-1");
	});

	it("a one-step macro is not summarized", async () => {
		api.updateMacro.mockResolvedValue(OLD_SERVER);
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do another thing", skills: [] },
		]);
		await save(w);
		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});

	it("a new macro of two steps is summarized", async () => {
		api.createMacro.mockResolvedValue({ data: { name: "MACRO-9", macro_name: "Brand new" } });
		api.summarizeMacro.mockResolvedValue({ ok: true });
		const w = mount(MacroDetail, {
			props: { id: "", isNew: true },
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		await nameField(w).vm.$emit("update:modelValue", "Brand new");
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await save(w);
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-9");
	});

	it("an answer of `summarize: false` is an answer, not a missing one", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await save(w);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});
});

describe("MacroDetail save: the user opened another macro before it answered", () => {
	// /macros/A to /macros/B is one component with new props. The save read the
	// macro's id AFTER its request, so a summary for A was started on B, and B's
	// freshly loaded form had its summary blanked and was then marked clean.
	const TWO = [
		{ label: "", prompt: "do the thing", skills: [] },
		{ label: "", prompt: "then this", skills: [] },
	];
	const summaryField = (w) =>
		w
			.findAllComponents({ name: "FormControl" })
			.find((c) => String(c.attributes("placeholder") || "").startsWith("No summary yet"));

	const leaveMidSave = async () => {
		api.getMacro.mockImplementation((id) =>
			Promise.resolve(
				id === "MACRO-2"
					? baseMacro({
							name: "MACRO-2",
							macro_name: "The other macro",
							merged_prompt: "The other macro's summary.",
							merge_status: "ready",
					  })
					: baseMacro({ merged_prompt: "Old summary.", merge_status: "ready" })
			)
		);
		let answer;
		api.updateMacro.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
		api.summarizeMacro.mockResolvedValue({ ok: true, conversation: "c1" });
		const w = mount(MacroDetail, {
			props: { id: "MACRO-1", isNew: false },
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await saveBtn(w).trigger("click");
		await w.setProps({ id: "MACRO-2" });
		await flushPromises();
		expect(summaryField(w).props("modelValue")).toBe("The other macro's summary.");
		api.getMacro.mockClear();
		answer({ data: { summarize: true } });
		await flushPromises();
		return w;
	};

	it("starts the summary for the macro that was saved", async () => {
		await leaveMidSave();
		expect(api.summarizeMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).toHaveBeenCalledWith("MACRO-1");
	});

	it("leaves the form of the macro now on screen alone", async () => {
		const w = await leaveMidSave();
		expect(summaryField(w).props("modelValue")).toBe("The other macro's summary.");
		// Not "Summarizing", and not reloaded on the strength of another macro's save.
		expect(runBtn(w).props("disabled")).toBe(false);
		expect(runReason(w).exists()).toBe(false);
		expect(api.getMacro).not.toHaveBeenCalled();
		// The toasts arrive over the OTHER macro's page: each names the one it is about.
		expect(toast.success).toHaveBeenCalledWith("Saved “Month-end close”");
		const started = toast.create.mock.calls.map((c) => c[0]).filter((t) => t.type === "info");
		expect(started).toHaveLength(1);
		expect(started[0].message).toContain("Summarizing “Month-end close”");
	});

	it("a new macro saved after the user left is not opened over where they went", async () => {
		let answer;
		api.createMacro.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
		api.getMacro.mockResolvedValue(baseMacro({ name: "MACRO-2" }));
		const w = mount(MacroDetail, {
			props: { id: "", isNew: true },
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		await nameField(w).vm.$emit("update:modelValue", "Brand new");
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await saveBtn(w).trigger("click");
		expect(api.createMacro).toHaveBeenCalledTimes(1);
		await w.setProps({ id: "MACRO-2", isNew: false });
		await flushPromises();
		answer({ data: { name: "MACRO-9", summarize: false } });
		await flushPromises();
		expect(router.replace).not.toHaveBeenCalled();
	});

	it("Re-summarize that answers after the user left does not mark the other macro", async () => {
		api.getMacro.mockImplementation((id) =>
			Promise.resolve(baseMacro({ name: id, steps: TWO, merge_status: "ready" }))
		);
		let answer;
		api.summarizeMacro.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
		const w = mount(MacroDetail, {
			props: { id: "MACRO-1", isNew: false },
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		w.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Re-summarize")
			.onClick();
		await w.setProps({ id: "MACRO-2" });
		await flushPromises();
		answer({ ok: true, conversation: "c1" });
		await flushPromises();
		expect(runBtn(w).props("disabled")).toBe(false);
	});
});

describe("MacroDetail: a summary that could not be started is said, with the way out", () => {
	// After a save it was silent: the form said "Saved" and nothing else. Since a
	// later save no longer retries (only a step change or Re-summarize starts a
	// summary), the owner has to be told, and told where the retry is.
	const TWO = [
		{ label: "", prompt: "do the thing", skills: [] },
		{ label: "", prompt: "then this", skills: [] },
	];
	const summaryField = (w) =>
		w
			.findAllComponents({ name: "FormControl" })
			.find((c) => String(c.attributes("placeholder") || "").startsWith("No summary yet"));
	const resummarizeOption = (w) =>
		w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Re-summarize");
	const saveChangedSteps = async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", TWO);
		await saveBtn(w).trigger("click");
		await flushPromises();
		return w;
	};
	const warnings = () =>
		toast.create.mock.calls.map((c) => c[0]).filter((t) => t.type === "warning");

	it("a refusal after a save is shown with its reason, and the macro reads as saved", async () => {
		api.summarizeMacro.mockRejectedValue(
			new Error(
				"You have reached your monthly usage limit, so this macro cannot be summarized."
			)
		);
		const w = await saveChangedSteps();
		expect(toast.success).toHaveBeenCalledWith("Saved “Month-end close”");
		expect(toast.error).not.toHaveBeenCalled();
		expect(warnings()).toHaveLength(1);
		expect(warnings()[0].message).toContain("monthly usage limit");
		expect(warnings()[0].message).toContain("Re-summarize");
		expect(warnings()[0].message).toContain("steps run in order");
		// Saved: Save is off, and Run is not waiting for a summary that is not coming.
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
		expect(runBtn(w).props("disabled")).toBe(false);
	});

	it("a busy site after a save is shown the same way", async () => {
		api.summarizeMacro.mockResolvedValue({ ok: false, reason: "The site is <busy>." });
		await saveChangedSteps();
		expect(warnings()).toHaveLength(1);
		// The toast renders HTML: the reason goes in escaped.
		expect(warnings()[0].message).toContain("The site is &lt;busy&gt;.");
		expect(warnings()[0].message).toContain("Re-summarize");
		expect(warnings()[0].message).toContain(
			"The summary of “Month-end close” was not started"
		);
	});

	it("the warning reads as sentences whether or not the reason ends in a full stop", async () => {
		const sentence = /was not started: (.*) Until there is one/;
		for (const [reason, shown] of [
			["Network Error", "Network Error."],
			["The site is busy.", "The site is busy."],
			["Try again in a few minutes!  ", "Try again in a few minutes!"],
		]) {
			vi.clearAllMocks();
			api.summarizeMacro.mockRejectedValue(new Error(reason));
			await saveChangedSteps();
			expect(warnings()).toHaveLength(1);
			expect(warnings()[0].message.match(sentence)[1]).toBe(shown);
		}
	});

	it("says nothing of the kind when the summary did start", async () => {
		api.summarizeMacro.mockResolvedValue({ ok: true, conversation: "c1" });
		await saveChangedSteps();
		expect(warnings()).toHaveLength(0);
	});

	it("the empty summary box no longer promises one on any save", async () => {
		const w = await mountDetail(baseMacro({ steps: TWO }));
		const placeholder = summaryField(w).attributes("placeholder");
		expect(placeholder).not.toContain("saving 2+ steps generates one");
		expect(placeholder).toContain("Re-summarize");
	});

	it("a failed summary says what happens now and how to try again", async () => {
		const failed = await mountDetail(baseMacro({ steps: TWO, merge_status: "failed" }));
		const note = failed.find('[data-testid="summary-failed"]');
		expect(note.text()).toContain("steps run in order");
		expect(note.text()).toContain("Re-summarize");
		const ready = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		expect(ready.find('[data-testid="summary-failed"]').exists()).toBe(false);
	});

	it("Re-summarize is off while the form has unsaved changes, and says why", async () => {
		// It summarizes the SAVED steps, not what is on screen.
		const w = await mountDetail(
			baseMacro({ steps: TWO, merged_prompt: "Both, as one.", merge_status: "ready" })
		);
		expect(resummarizeOption(w).disabled).toBeFalsy();
		expect(resummarizeOption(w).description).toBeUndefined();
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(resummarizeOption(w).disabled).toBe(true);
		expect(resummarizeOption(w).description).toContain("Save your changes first");
		// A disabled menu item may never be reached with a keyboard or read out, so
		// its description is not where the reason lives: the line under the buttons
		// names Re-summarize too, and the menu's own button points at it.
		expect(runReason(w).text()).toContain("Run and Re-summarize use the saved macro");
		expect(menuBtn(w).attributes("aria-describedby")).toBe(runReason(w).attributes("id"));
		// Refused inside too, not only by the disabled item.
		await resummarizeOption(w).onClick();
		await flushPromises();
		expect(api.summarizeMacro).not.toHaveBeenCalled();
	});
});

describe("MacroDetail: the line under the buttons speaks for Re-summarize too", () => {
	const TWO = [
		{ label: "", prompt: "do the thing", skills: [] },
		{ label: "", prompt: "then this", skills: [] },
	];

	it("does not name Re-summarize on a macro that has no such menu item", async () => {
		// One step: nothing to summarize, so the menu does not offer it.
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(runReason(w).text()).toContain("Save your changes first");
		expect(runReason(w).text()).not.toContain("Re-summarize");
		expect(menuBtn(w).attributes("aria-describedby")).toBeUndefined();
	});

	it("the menu button points nowhere while Re-summarize is on", async () => {
		const w = await mountDetail(baseMacro({ steps: TWO, merge_status: "ready" }));
		expect(menuBtn(w).attributes("aria-describedby")).toBeUndefined();
		// Summarizing, form clean: Re-summarize is on (it replaces the pending one).
		const pending = await mountDetail(baseMacro({ steps: TWO, merge_status: "pending" }));
		expect(menuBtn(pending).attributes("aria-describedby")).toBeUndefined();
		expect(runReason(pending).text()).not.toContain("Re-summarize");
	});

	it("says both reasons while a summary is being written and the form is edited", async () => {
		// "Summarizing" used to be the whole line, and Re-summarize was off without a word.
		const w = await mountDetail(baseMacro({ steps: TWO, merge_status: "pending" }));
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		expect(runReason(w).text()).toContain("Summarizing");
		expect(runReason(w).text()).toContain("Save your changes before Re-summarize");
		expect(menuBtn(w).attributes("aria-describedby")).toBe(runReason(w).attributes("id"));
	});
});

describe("MacroDetail save: what is missing is said on the field", () => {
	const nameError = (w) => w.find(".error-message");

	it("marks Name as required", async () => {
		const w = await mountDetail(baseMacro());
		expect(nameField(w).attributes("required")).toBeDefined();
	});

	it("an empty name blocks the save with a message under the field", async () => {
		const w = await mountDetail(baseMacro());
		await nameField(w).vm.$emit("update:modelValue", "   ");
		expect(nameError(w).exists()).toBe(false);
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro).not.toHaveBeenCalled();
		expect(nameError(w).text()).toBe("Give the macro a name.");
		expect(toast.error).toHaveBeenCalledTimes(1);
		// It clears the moment the field has a value again.
		await nameField(w).vm.$emit("update:modelValue", "Close the month");
		expect(nameError(w).exists()).toBe(false);
	});

	it("a step with a label and no prompt blocks the save, marked on that step", async () => {
		// It used to be dropped from the save without a word.
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "Post the entries", prompt: "  ", skills: [] },
		]);
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro).not.toHaveBeenCalled();
		expect(stepsBuilder(w).props("errors")).toEqual({
			1: "Add a prompt for this step, or remove the step.",
		});
		expect(toast.error).toHaveBeenCalledWith("Step 2 needs a prompt.");
	});

	it("a macro's only step is not told to 'remove the step': that cannot be done", async () => {
		// Remove is off while there is one step; the way out is to clear the label.
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "Post the entries", prompt: "", skills: [] },
		]);
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(stepsBuilder(w).props("errors")).toEqual({
			0: "Add a prompt for this step, or clear its label.",
		});
	});

	it("a mark stays on its step when the steps are reordered", async () => {
		// The marks used to be dropped on any reorder, add or remove: the step was
		// still wrong and nothing said so until the next Save.
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "Post the entries", prompt: "", skills: [] },
			{ label: "", prompt: "and report", skills: [] },
		]);
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(Object.keys(stepsBuilder(w).props("errors"))).toEqual(["1"]);

		// StepsBuilder emits a new array of the same step objects.
		const [a, b, c] = stepsBuilder(w).props("modelValue");
		await stepsBuilder(w).vm.$emit("update:modelValue", [b, a, c]);
		await flushPromises();
		expect(stepsBuilder(w).props("errors")).toEqual({
			0: "Add a prompt for this step, or remove the step.",
		});

		// A step added: the mark does not move, and the new blank card gets none.
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			...stepsBuilder(w).props("modelValue"),
			{ label: "", prompt: "", skills: [] },
		]);
		await flushPromises();
		expect(Object.keys(stepsBuilder(w).props("errors"))).toEqual(["0"]);

		// Another step removed: the mark follows its step to the new position.
		const now = stepsBuilder(w).props("modelValue");
		await stepsBuilder(w).vm.$emit("update:modelValue", [now[1], now[0]]);
		await flushPromises();
		expect(Object.keys(stepsBuilder(w).props("errors"))).toEqual(["1"]);

		// The marked step itself removed: nothing left to mark.
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			stepsBuilder(w).props("modelValue")[0],
		]);
		await flushPromises();
		expect(stepsBuilder(w).props("errors")).toEqual({});
	});

	it("the mark goes once the step has a prompt, and the save goes through", async () => {
		api.updateMacro.mockResolvedValue({});
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "Post the entries", prompt: "", skills: [] },
		]);
		await saveBtn(w).trigger("click");
		await flushPromises();
		// Typing mutates the step in place, exactly as StepsBuilder does.
		stepsBuilder(w).props("modelValue")[1].prompt = "post them";
		await flushPromises();
		expect(stepsBuilder(w).props("errors")).toEqual({});
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro.mock.calls[0][0].steps.map((s) => s.prompt)).toEqual([
			"do the thing",
			"post them",
		]);
	});

	it("a wholly empty step is not a change, and is left out of the save", async () => {
		api.updateMacro.mockResolvedValue({});
		const w = await mountDetail(baseMacro());
		await stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: " ", prompt: "", skills: [] },
		]);
		// The blank card the builder keeps to type into: nothing to save yet.
		expect(saveBtn(w).attributes("disabled")).toBeDefined();
		await nameField(w).vm.$emit("update:modelValue", "Renamed macro");
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(api.updateMacro.mock.calls[0][0].steps).toHaveLength(1);
		expect(stepsBuilder(w).props("errors")).toEqual({});
	});
});

describe("MacroDetail Enabled switch: says what switching it off does", () => {
	it("skips scheduled runs, and the macro can still be run by hand", async () => {
		// macros.run_macro refuses a disabled macro only for trigger == "scheduled".
		const w = await mountDetail(baseMacro());
		const text = w
			.findAllComponents({ name: "Switch" })
			.find((c) => c.props("label") === "Enabled")
			.props("description");
		expect(text).toContain("scheduled runs are skipped");
		expect(text).toContain("by hand");
		expect(text).not.toContain("won't run");
		expect(text).not.toContain("chat run menu");
	});
});

describe("MacroDetail delete: the macro's name is text, not markup", () => {
	it("is escaped on its way into the confirmation, which renders HTML", async () => {
		// frappe-ui's ConfirmDialog binds `message` with v-html.
		const w = await mountDetail(
			baseMacro({ macro_name: 'Close <img src=x onerror="go()"> & go' })
		);
		const del = w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Delete");
		del.onClick();
		const { message } = confirmDialog.mock.calls[0][0];
		expect(message).toContain("Close &lt;img src=x onerror=&quot;go()&quot;&gt; &amp; go");
		expect(message).not.toContain("<img");
	});
});

describe("MacroDetail Schedule section: the summary says whose clock the time is on", () => {
	const scheduled = { next_run_at: "2026-10-02 09:00:00" };

	it("names the site timezone beside the time", async () => {
		// 9:00 am is stored as a site-zone time and shown as stored; "Next run" beside
		// it is converted to the viewer's zone. Unlabelled, the two read as one clock.
		siteConfig.systemTimezone = "Asia/Kolkata";
		const w = await mountDetail(baseMacro(scheduled));
		expect(w.text()).toContain("Scheduled daily at 9:00 am, Asia/Kolkata time. Next run:");
	});

	it("adds nothing to Next run when the viewer is on the site's clock", async () => {
		siteConfig.systemTimezone = "Asia/Kolkata";
		const w = await mountDetail(baseMacro(scheduled));
		expect(w.text()).not.toContain("your time");
	});

	it("labels Next run 'your time' when the viewer's clock differs from the site's", async () => {
		// A London viewer of a Kolkata site: one sentence, two clocks, both labelled.
		siteConfig.systemTimezone = "Asia/Kolkata";
		siteConfig.viewerClock = "2026-10-02 04:30:00";
		const w = await mountDetail(baseMacro(scheduled));
		expect(w.text()).toContain(
			"Scheduled daily at 9:00 am, Asia/Kolkata time. Next run: 2026-10-02 04:30:00 (your time)"
		);
	});

	it("labels a retry time the same way", async () => {
		siteConfig.systemTimezone = "Asia/Kolkata";
		siteConfig.viewerClock = "2026-10-01 05:47:04";
		const w = await mountDetail(
			baseMacro({ next_run_at: "2026-10-01 11:17:04", next_run_is_retry: 1 })
		);
		expect(w.text()).toContain("Retrying: 2026-10-01 05:47:04 (your time)");
	});

	it("keeps the old text when the site timezone is not known", async () => {
		const w = await mountDetail(baseMacro(scheduled));
		expect(w.text()).toContain("Scheduled daily at 9:00 am. Next run:");
	});
});

describe("MacroDetail: an admin's hold", () => {
	const HELD_SAYS =
		"An admin has put this macro on hold: Sends too many emails. It will not run until an admin releases it.";
	// The full sentence is the banner's, said once; the controls it turns off say this.
	const HELD_SHORT = "On hold: only an admin can release it.";
	const held = (extra = {}) =>
		baseMacro({
			enabled: 0,
			schedule_enabled: 0,
			skip_confirmation: 0,
			admin_hold: 1,
			admin_hold_reason: "Sends too many emails",
			...extra,
		});
	const switchOf = (w, label) =>
		w.findAllComponents({ name: "Switch" }).find((s) => s.props("label").startsWith(label));

	it("shows the banner with the admin's reason", async () => {
		const w = await mountDetail(held());
		const banner = w.find('[data-testid="hold-banner"]');
		expect(banner.exists()).toBe(true);
		expect(banner.text()).toContain("On hold");
		expect(banner.text()).toContain(HELD_SAYS);
	});

	it("keeps Run off and says why, on the line the button points to", async () => {
		const w = await mountDetail(held());
		expect(runBtn(w).props("disabled")).toBe(true);
		expect(runBtn(w).props("tooltip")).toBe(HELD_SHORT);
		expect(runReason(w).text()).toBe(HELD_SHORT);
		// The full sentence is on the page once.
		expect(w.text().split(HELD_SAYS)).toHaveLength(2);
		await runBtn(w).trigger("click");
		expect(api.runMacro).not.toHaveBeenCalled();
	});

	it("keeps Enabled, the schedule and the arm switch off, each with the reason", async () => {
		window.is_jarvis_admin = true; // the arm switch would otherwise be off for its own reason
		try {
			const w = await mountDetail(held());
			for (const label of ["Enabled", "Run on a schedule", "Skip confirmation"]) {
				const sw = switchOf(w, label);
				expect(sw.props("disabled"), label).toBe(true);
				expect(sw.props("description"), label).toBe(HELD_SHORT);
			}
			// Everything else stays the owner's to edit.
			expect(switchOf(w, "Stop on error").props("disabled")).toBe(false);
		} finally {
			delete window.is_jarvis_admin;
		}
	});

	it("still lets a switch that is on be switched off", async () => {
		const w = await mountDetail(held({ enabled: 1 }));
		const enabled = switchOf(w, "Enabled");
		expect(enabled.props("disabled")).toBe(false);
		expect(enabled.props("description")).not.toBe(HELD_SHORT);
	});

	const TWO = [
		{ label: "", prompt: "do the thing", skills: [] },
		{ label: "", prompt: "then this", skills: [] },
	];
	const menuItem = (w, label) =>
		w
			.findAllComponents({ name: "Dropdown" })
			.map((d) => d.props("options") || [])
			.flat()
			.find((o) => o.label === label);

	it("the owner can still edit the steps and save", async () => {
		api.updateMacro.mockResolvedValue({ data: { summarize: false } });
		const w = await mountDetail(held());
		stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the other thing", skills: [] },
		]);
		await flushPromises();
		expect(saveBtn(w).attributes("disabled")).toBeUndefined();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(api.updateMacro.mock.calls[0][0].steps[0].prompt).toBe("do the other thing");
		expect(toast.success).toHaveBeenCalled();
	});

	it("starts no summary after a save while held, whatever the answer says", async () => {
		// The server refuses a summary of a held macro and answers summarize: false;
		// the form does not ask either.
		api.updateMacro.mockResolvedValue({ data: { summarize: true } });
		const w = await mountDetail(held({ steps: TWO }));
		stepsBuilder(w).vm.$emit("update:modelValue", [
			{ label: "", prompt: "do the thing", skills: [] },
			{ label: "", prompt: "then that", skills: [] },
		]);
		await flushPromises();
		await saveBtn(w).trigger("click");
		await flushPromises();
		expect(api.updateMacro).toHaveBeenCalledTimes(1);
		expect(api.summarizeMacro).not.toHaveBeenCalled();
		expect(toast.create).not.toHaveBeenCalled();
	});

	it("keeps Re-summarize off, with why", async () => {
		const w = await mountDetail(held({ steps: TWO }));
		const item = menuItem(w, "Re-summarize");
		expect(item.disabled).toBe(true);
		expect(item.description).toBe(HELD_SHORT);
	});

	it("the owner can still delete it", async () => {
		const w = await mountDetail(held());
		const item = menuItem(w, "Delete");
		expect(item.disabled).toBeFalsy();
		item.onClick();
		const dialog = confirmDialog.mock.calls.at(-1)[0];
		api.deleteMacro.mockResolvedValue({ ok: true, stopped_runs: 0 });
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(api.deleteMacro).toHaveBeenCalledWith("MACRO-1");
	});

	it("shows nothing of the kind for a macro that is not held, or from an older server", async () => {
		for (const fixture of [baseMacro({ admin_hold: 0, admin_hold_reason: "" }), baseMacro()]) {
			const w = await mountDetail(fixture);
			expect(w.find('[data-testid="hold-banner"]').exists()).toBe(false);
			expect(runBtn(w).props("disabled")).toBe(false);
			expect(switchOf(w, "Enabled").props("disabled")).toBe(false);
		}
	});
});
