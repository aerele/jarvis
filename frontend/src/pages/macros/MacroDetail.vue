<template>
	<DocPage
		:breadcrumbs="breadcrumbs"
		:title="pageTitle"
		:status-badge="null"
		:dirty="dirty"
		:loading="loading"
		:error="loadError"
	>
		<template #actions>
			<Button
				v-if="!isNew"
				label="Run"
				iconLeft="play"
				:loading="running"
				:disabled="mergePending || running || dirty"
				:tooltip="runBlockedReason || 'Run this macro now'"
				:aria-describedby="runBlockedReason ? RUN_REASON_ID : undefined"
				@click="run"
			/>
			<Dropdown v-if="!isNew" :options="overflowOptions">
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
			<Button
				variant="solid"
				label="Save"
				:disabled="!dirty"
				:loading="saving"
				@click="save"
			/>
		</template>

		<template #main>
			<!-- Why Run is off, as text. The tooltip alone does not reach everyone: a
			     disabled button takes no keyboard focus, a screen reader gets "Run,
			     dimmed" and no reason, and touch has no hover. The button points here
			     with aria-describedby (the pattern of approvals/SheetDetail.vue).
			     A saved macro keeps one line's room for it whether or not it shows:
			     it appears on the first keystroke, and without the room every field
			     below moved down under the pointer. -->
			<div v-if="!isNew" data-testid="run-reason-room" class="mb-4 min-h-5">
				<p
					v-if="runBlockedReason"
					:id="RUN_REASON_ID"
					data-testid="run-reason"
					class="text-sm text-ink-amber-3"
				>
					{{ runBlockedReason }}
				</p>
			</div>
			<!-- How the last run went, when the owner needs telling: it failed, it is
			     waiting on them, or a step only drafted a record. Nothing else on this
			     page said, and a scheduled run fails with nobody watching. -->
			<Banner
				v-if="lastRunNote"
				class="mb-4"
				data-testid="last-run-note"
				:type="LAST_RUN_BANNER[lastRunNote.tone] || 'info'"
				:title="lastRunNote.title"
				:message="lastRunNote.text"
			>
				<template v-if="lastRun && lastRun.conversation" #action>
					<Button
						variant="ghost"
						label="Open the chat"
						@click="router.push('/c/' + lastRun.conversation)"
					/>
				</template>
			</Banner>
			<DocSection label="Details">
				<div class="space-y-4">
					<div>
						<FormControl
							type="text"
							label="Name"
							required
							placeholder="e.g. Monthly close"
							:modelValue="form.macro_name"
							:disabled="saving"
							:aria-invalid="fieldErrors.macro_name ? 'true' : undefined"
							@update:modelValue="(v) => (form.macro_name = v)"
						/>
						<ErrorMessage :message="fieldErrors.macro_name" class="mt-2" />
					</div>
					<FormControl
						type="textarea"
						label="Description"
						:rows="2"
						placeholder="What does this macro do?"
						:modelValue="form.description"
						:disabled="saving"
						@update:modelValue="(v) => (form.description = v)"
					/>
					<Switch
						v-model="form.enabled"
						label="Enabled"
						description="Off = saved as a draft - its scheduled runs are skipped. You can still run it by hand."
						:disabled="saving"
					/>
					<Switch
						v-model="form.stop_on_error"
						label="Stop on error"
						description="Stop the chain if a step fails - otherwise it keeps going after an error."
						:disabled="saving"
					/>
					<Switch
						v-model="form.skip_confirmation"
						label="Skip confirmation (run writes uncarded)"
						:description="
							canArm
								? 'Admin only. This macro\'s runs execute writes WITHOUT a confirmation card - including run_method, send_email and run_import. delete/cancel/amend still park and stop the run. Arming trusts the owner for current and future steps.'
								: 'Admin only - a Jarvis Admin or System Manager can arm this macro to run its writes without a confirmation card.'
						"
						:disabled="saving || !canArm"
					/>
				</div>
			</DocSection>

			<DocSection label="Schedule">
				<div class="space-y-4">
					<!-- An account the scheduler refuses (Administrator) is told so here
					     and cannot switch a schedule ON; the server refuses that save
					     too. Switching an existing schedule OFF stays possible. -->
					<Switch
						v-model="form.schedule_enabled"
						label="Run on a schedule"
						:description="
							scheduleBlocked || `${agentName} runs this macro automatically.`
						"
						:disabled="saving || (!!scheduleBlocked && !form.schedule_enabled)"
					/>
					<div v-if="form.schedule_enabled" class="flex items-start gap-4">
						<FormControl
							class="flex-1"
							type="select"
							label="Frequency"
							:options="FREQUENCY_OPTIONS"
							:modelValue="form.schedule_frequency"
							:disabled="saving"
							@update:modelValue="(v) => (form.schedule_frequency = v)"
						/>
						<div class="flex-1">
							<label class="mb-1.5 block text-xs text-ink-gray-5">Time</label>
							<TimePicker
								v-model="form.schedule_time"
								placeholder="09:00"
								:disabled="saving"
							/>
						</div>
						<!-- weekly -> weekday name, monthly -> day of month; hidden for
						     daily. Copies the Frequency control above exactly - jarvis#653.
						     Monthly's 31 rows overflow this Select's popover with no cap of
						     its own (frappe-ui's Select sets no max-height on its content,
						     unlike Combobox/Autocomplete/MultiSelect) - the fix lives in
						     src/main.css's [role="listbox"] [data-slot="content-body"]
						     rule, app-wide for every plain Select, not here (nothing to add
						     on this control itself). -->
						<FormControl
							v-if="form.schedule_frequency !== 'daily'"
							class="flex-1"
							type="select"
							label="Day"
							:options="dayOptions"
							:modelValue="form.schedule_day"
							:disabled="saving"
							@update:modelValue="(v) => (form.schedule_day = v)"
						/>
					</div>
					<div v-if="scheduleSummary" class="text-sm text-ink-gray-5">
						{{ scheduleSummary }}
					</div>
				</div>
			</DocSection>

			<DocSection label="Steps">
				<StepsBuilder v-model="form.steps" :disabled="saving" :errors="stepErrors" />
			</DocSection>

			<DocSection
				v-if="!isNew"
				label="Summarized prompt"
				:opened="!!form.merged_prompt || mergeStatus === 'pending'"
			>
				<template #header-suffix>
					<Badge
						v-if="mergeStatus === 'pending'"
						variant="subtle"
						theme="orange"
						label="Generating…"
					/>
					<Badge
						v-else-if="mergeStatus === 'ready'"
						variant="subtle"
						theme="gray"
						label="Ready"
					/>
					<Badge
						v-else-if="mergeStatus === 'failed'"
						variant="subtle"
						theme="red"
						label="Failed"
					/>
				</template>
				<FormControl
					type="textarea"
					:rows="9"
					class="font-mono"
					placeholder="No summary yet - saving 2+ steps generates one in the background."
					description="When present, runs use this prompt instead of the steps."
					:modelValue="form.merged_prompt"
					:disabled="saving || mergePending"
					@update:modelValue="(v) => (form.merged_prompt = v)"
				/>
			</DocSection>
		</template>

		<template #aside>
			<!-- new-record mode: metadata exists only after the first save -->
			<div v-if="isNew" class="m-5 rounded-md border p-4 text-sm text-ink-gray-6">
				Save to enable comments, assignees and attachments.
			</div>
			<DocMetaPanel v-else-if="docmeta" :docmeta="docmeta" :can-write="true" />
		</template>

		<template #footer>
			<CommentsSection v-if="!isNew && docmeta" :docmeta="docmeta" :can-comment="true" />
		</template>
	</DocPage>
</template>

<script setup>
// Macro detail + create page (DESIGN-V3 §6.3): /macros/:id and /macros/new
// share this component (isNew prop). Explicit Save (D21) porting round-2
// MacrosView's exact save/summary semantics (steps-touched vs merged-touched),
// StepsBuilder drag editor, schedule section, summarized-prompt section with
// merge-status badge, Run gated while summarizing, prefill hand-off from the
// chat's Save-as-macro (takeMacroPrefill), DocMetaPanel + comments, dirty guard.
import {
	ref,
	reactive,
	computed,
	watch,
	nextTick,
	shallowRef,
	toRaw,
	inject,
	onMounted,
	onBeforeUnmount,
} from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Button,
	Badge,
	Dropdown,
	ErrorMessage,
	FormControl,
	Switch,
	TimePicker,
	toast,
	confirmDialog,
} from "frappe-ui";
import DocPage from "@/components/doc/DocPage.vue";
import Banner from "@/components/Banner.vue";
import DocSection from "@/components/doc/DocSection.vue";
import DocMetaPanel from "@/components/doc/DocMetaPanel.vue";
import CommentsSection from "@/components/doc/CommentsSection.vue";
import { useDocmeta } from "@/composables/useDocmeta";
import StepsBuilder from "./StepsBuilder.vue";
import { takeMacroPrefill } from "@/composables/macroPrefill";
import { exactDate, formatTime12h, onAnotherClock, siteTimezone } from "@/utils/datetime";
import {
	WEEKDAY_OPTIONS,
	DAY_OF_MONTH_OPTIONS,
	deriveScheduleDay,
	scheduleAnchorPhrase,
} from "@/lib/scheduleAnchor";
import * as api from "@/api";
import { agentName } from "@/branding";
import { errMessage as errMsg, errHtml, escapeHtml } from "@/lib/errors";
import { session } from "@/data/session";
import { cannotScheduleReason } from "@/lib/macroSchedule";
import { lastRunLine, deleteWarning, stoppedRunsNote } from "@/lib/macroRunOutcome";

const props = defineProps({
	id: { type: String, default: "" },
	isNew: { type: Boolean, default: false },
});

const router = useRouter();
const socket = inject("$socket");
const DOCTYPE = "Jarvis Macro";

const FREQUENCY_OPTIONS = [
	{ label: "Daily", value: "daily" },
	{ label: "Weekly", value: "weekly" },
	{ label: "Monthly", value: "monthly" },
];

// ── state ────────────────────────────────────────────────────────────────────
const macro = ref(null); // last server copy (null while new)
const loading = ref(false);
const loadError = ref("");
const saving = ref(false);
const running = ref(false);
const mergeStatus = ref(""); // '' | 'pending' | 'ready' | 'failed'
const nextRunRaw = ref("");
// The server's `last_run` for this macro (null when it never ran, undefined from a
// server that does not send it yet).
const lastRun = ref(null);
const lastRunNote = computed(() => lastRunLine(lastRun.value));
const LAST_RUN_BANNER = { bad: "error", warn: "warning" };
const nextRunIsRetry = ref(false);

const form = reactive({
	macro_name: "",
	description: "",
	enabled: true,
	stop_on_error: true,
	skip_confirmation: false,
	schedule_enabled: false,
	schedule_frequency: "daily",
	schedule_time: "09:00",
	schedule_day: "",
	steps: [],
	merged_prompt: "",
});
// jarvis#653: weekly -> weekday names, monthly -> 1..31 (ordinal labels) - the
// Day control's own option list, keyed off the DRAFT frequency so it flips the
// instant Frequency changes, before any save.
const dayOptions = computed(() =>
	form.schedule_frequency === "weekly" ? WEEKDAY_OPTIONS : DAY_OF_MONTH_OPTIONS
);
// jarvis#653: guards the interactive-frequency-change watcher below from firing
// off `seed()`'s own frequency+day assignment (which legitimately sets day to
// "" for a legacy row with no anchor saved - the defaulting watcher must not
// treat that as "needs a default"). True for exactly the synchronous pre-flush
// window seed()'s own assignment runs in; nextTick clears it once the DOM
// update (and any watcher chained off THIS assignment) has settled. Mirrors
// AgentDetail.vue's `seedingSchedule` guard exactly.
let seedingSchedule = false;
// jarvis#653: an interactive Frequency change (NOT `seed()`, guarded off by
// `seedingSchedule`) needs its OWN default day - "Monday" for weekly, the 1st
// for monthly - so the Day select never shows a visible option ("Monday")
// while the model is empty (a phantom selection that would silently save
// nothing). Only fills in when the current day does not already belong to the
// new frequency's own option set, so switching away and back keeps whatever
// was picked.
watch(
	() => form.schedule_frequency,
	(freq) => {
		if (seedingSchedule) return;
		if (freq === "weekly" && !WEEKDAY_OPTIONS.some((o) => o.value === form.schedule_day)) {
			form.schedule_day = "Monday";
		} else if (
			freq === "monthly" &&
			!DAY_OF_MONTH_OPTIONS.some((o) => o.value === form.schedule_day)
		) {
			form.schedule_day = "1";
		}
	}
);
// Saved-state copy for the dirty compare (set by seed). MUST be a ref: on
// /macros/:id the dirty computed first evaluates while the load is in flight,
// and with a plain variable the `!snapshot` short-circuit would track zero
// reactive deps - the computed caches false and never re-runs, so Save never
// enables on existing macros.
const snapshot = ref(null);

// Inline save errors (TriggerDetail's pattern: an ErrorMessage under the field,
// set all at once by save(), each clearing itself the moment its field is put
// right). Toast-only, the owner was told once and then had to find the field.
const fieldErrors = reactive({ macro_name: "" });
watch(
	() => form.macro_name,
	() => {
		if (String(form.macro_name || "").trim()) fieldErrors.macro_name = "";
	}
);
// The same for steps, keyed by the step's position in form.steps (StepsBuilder
// shows each under its own step). Typing only ever CLEARS a mark (a step being
// filled in is not nagged before the next save). A mark belongs to its STEP, not
// to a position: StepsBuilder answers a move, an add or a remove with a new array
// of the same step objects, and the marks are carried over to wherever their
// steps now sit. They used to be dropped, so reordering to "fix" things made the
// red text vanish from a step that was still wrong.
// The message offers "remove the step" only when that can be done: the builder
// will not remove a macro's only step, and there the way out is the label.
const STEP_NEEDS_PROMPT = "Add a prompt for this step, or remove the step.";
const SOLE_STEP_NEEDS_PROMPT = "Add a prompt for this step, or clear its label.";
const stepErrors = ref({});
watch(
	() => form.steps,
	(steps, was) => {
		const marked = Object.keys(stepErrors.value);
		if (!marked.length) return;
		const flagged = new Set(marked.map((i) => toRaw((was || [])[i])));
		stepErrors.value = Object.fromEntries(
			Object.entries(promptlessSteps(steps)).filter(([i]) => flagged.has(toRaw(steps[i])))
		);
	},
	{ deep: true }
);

const docmeta = shallowRef(null); // useDocmeta instance (persisted macros only)

const mergePending = computed(() => mergeStatus.value === "pending");
// Why Run is off, "" when it is not (or only because a run is being started).
// One sentence for the visible line, the tooltip and the button's description.
const RUN_REASON_ID = "macro-run-reason";
const runBlockedReason = computed(() => {
	if (mergePending.value) return "Summarizing… Run unlocks when the summary is ready.";
	if (dirty.value) {
		return "Save your changes first. Run starts the saved macro, not what is on screen.";
	}
	return "";
});
const stepsWithPrompt = computed(() => form.steps.filter((s) => (s.prompt || "").trim()).length);

// Arming a macro to skip confirmation is admin-only (the backend re-checks
// require_jarvis_admin, so this is a UX gate, not the security boundary); a
// non-admin sees the toggle disabled with the reason instead of a throw-on-save.
const canArm = !!(window.is_jarvis_admin || window.is_system_manager);

const dirty = computed(() => {
	const snap = snapshot.value;
	if (!snap || loading.value) return false;
	return (
		(form.macro_name || "") !== snap.macro_name ||
		(form.description || "") !== snap.description ||
		(form.enabled ? 1 : 0) !== snap.enabled ||
		(form.stop_on_error ? 1 : 0) !== snap.stop_on_error ||
		(form.skip_confirmation ? 1 : 0) !== snap.skip_confirmation ||
		(form.schedule_enabled ? 1 : 0) !== snap.schedule_enabled ||
		(form.schedule_frequency || "daily") !== snap.schedule_frequency ||
		(form.schedule_time || "") !== snap.schedule_time ||
		(form.schedule_day || "") !== (snap.schedule_day || "") ||
		JSON.stringify(cleanSteps(form.steps)) !== snap.stepsJson ||
		(form.merged_prompt || "") !== snap.merged_prompt
	);
});

const pageTitle = computed(() =>
	props.isNew ? form.macro_name || "New Macro" : form.macro_name || props.id
);
const breadcrumbs = computed(() => [
	{ label: "Macros", route: { name: "MacrosList" } },
	props.isNew
		? { label: "New Macro", route: { name: "MacroNew" } }
		: { label: pageTitle.value, route: { name: "MacroDetail", params: { id: props.id } } },
]);

const nextRunAt = computed(() => exactDate(nextRunRaw.value));
// Why this macro cannot be on a schedule, or "" when it can. For a saved macro the
// SERVER says, judged from the macro's owner (the identity the scheduler runs as),
// so the warning and the save refusal can never disagree. A new macro has no owner
// yet: it will be whoever saves it, so the logged-in account is the right question.
const ownerBlockedReason = ref("");
const scheduleBlocked = computed(() =>
	props.isNew ? cannotScheduleReason(session.user) : ownerBlockedReason.value
);
// "Scheduled monthly on the 15th at 9:00 am. Next run: ..." - built from the
// SAVED snapshot only (never the live draft), mirroring AgentDetail.vue's own
// scheduleSummary exactly: blank while `dirty` (would narrate a schedule that
// no longer matches the form - nextRunAt never recomputes off an unsaved
// edit, it only ever changes on the next load()/seed()), when there is no
// next_run_at, or when the SAVED schedule is off.
const scheduleSummary = computed(() => {
	const snap = snapshot.value;
	if (!snap || props.isNew || dirty.value || !snap.schedule_enabled) return "";
	if (!nextRunAt.value) return "";
	const anchor = scheduleAnchorPhrase(snap.schedule_frequency, snap.schedule_day);
	// A failed scheduled run is retried at a time the owner did not choose; say so,
	// or "daily at 9:00 am. Next run: 11:17 am" reads as a contradiction.
	// The time is the stored one, on the SITE's clock; the next-run stamp after it
	// is shown on the viewer's. Naming the site zone, and marking the stamp "your
	// time" when the viewer's clock differs, keeps the two from reading as one
	// clock. A viewer on the site's clock gets no extra label, and when the shell
	// was not told the zone the old text stands.
	const zone = siteTimezone();
	const stamp =
		nextRunAt.value + (zone && onAnotherClock(nextRunRaw.value) ? " (your time)" : "");
	const next = nextRunIsRetry.value
		? `The last scheduled run failed (Runs, on the Macros page, says why). Retrying: ${stamp}`
		: `Next run: ${stamp}`;
	return (
		`Scheduled ${snap.schedule_frequency}${anchor ? ` ${anchor}` : ""} ` +
		`at ${formatTime12h(snap.schedule_time)}${zone ? `, ${zone} time` : ""}. ${next}`
	);
});

const overflowOptions = computed(() => {
	const opts = [];
	if (stepsWithPrompt.value >= 2) {
		opts.push({ label: "Re-summarize", icon: "refresh-cw", onClick: resummarize });
	}
	opts.push({ label: "Delete", icon: "trash-2", theme: "red", onClick: confirmDelete });
	return opts;
});

// ── step helpers ─────────────────────────────────────────────────────────────
function mapSteps(steps) {
	return (Array.isArray(steps) ? steps : []).map((s) => ({
		label: s.label || "",
		prompt: s.prompt || "",
		skills: Array.isArray(s.skills) ? [...s.skills] : [],
		// per-step overrides aren't editable here but must survive a save
		...(s.model_override ? { model_override: s.model_override } : {}),
		...(s.thinking_override ? { thinking_override: s.thinking_override } : {}),
	}));
}

// What a save would send. A wholly empty step (no label, no prompt) is the blank
// card the builder keeps to type into: left out, and not a change. A step with a
// label and no prompt STAYS: it is unsaved work (so the form is dirty), and save()
// refuses it by name. It used to be filtered out here, so it vanished on save
// without a word. The server makes the same split (macros_api._parse_steps).
function cleanSteps(steps) {
	return (steps || [])
		.map((s) => ({
			label: (s.label || "").trim(),
			prompt: (s.prompt || "").trim(),
			skills: Array.isArray(s.skills) ? s.skills : [],
			...(s.model_override ? { model_override: s.model_override } : {}),
			...(s.thinking_override ? { thinking_override: s.thinking_override } : {}),
		}))
		.filter((s) => s.prompt || s.label);
}

// { position in `steps`: message } for every step with a label and no prompt.
function promptlessSteps(steps) {
	const out = {};
	const message = (steps || []).length > 1 ? STEP_NEEDS_PROMPT : SOLE_STEP_NEEDS_PROMPT;
	(steps || []).forEach((s, i) => {
		if ((s.label || "").trim() && !(s.prompt || "").trim()) out[i] = message;
	});
	return out;
}

function toHHMM(t) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}

// ── load / init (re-runs when /macros/new saves and replaces to /macros/:id) ─
function seed(data) {
	// jarvis#653: guard the frequency-change watcher above from the assignment
	// two lines down - a legacy row with schedule_frequency weekly/monthly and
	// NO anchor saved must seed schedule_day as "" (matching the snapshot below
	// verbatim), not have the watcher fabricate "Monday"/"1" and leave the page
	// dirty before the owner has touched anything.
	seedingSchedule = true;
	form.macro_name = data.macro_name || "";
	form.description = data.description || "";
	form.enabled = data.enabled == null ? true : !!data.enabled;
	form.stop_on_error = !!data.stop_on_error;
	form.skip_confirmation = !!data.skip_confirmation;
	form.schedule_enabled = !!data.schedule_enabled;
	form.schedule_frequency = data.schedule_frequency || "daily";
	form.schedule_time = toHHMM(data.schedule_time) || "09:00";
	form.schedule_day = deriveScheduleDay(
		form.schedule_frequency,
		data.schedule_weekday,
		data.schedule_day_of_month
	);
	nextTick(() => {
		seedingSchedule = false;
	});
	form.steps = mapSteps(data.steps);
	if (!form.steps.length) form.steps = [{ label: "", prompt: "", skills: [] }];
	nextRunIsRetry.value = !!data.next_run_is_retry;
	form.merged_prompt = data.merged_prompt || "";
	mergeStatus.value = data.merge_status || "";
	nextRunRaw.value = data.next_run_at || "";
	ownerBlockedReason.value = data.schedule_blocked_reason || "";
	lastRun.value = data.last_run || null;
	snapshot.value = formSnapshot();
}

// The form as it stands, in the shape `dirty` compares against.
function formSnapshot() {
	return {
		macro_name: form.macro_name,
		description: form.description,
		enabled: form.enabled ? 1 : 0,
		stop_on_error: form.stop_on_error ? 1 : 0,
		skip_confirmation: form.skip_confirmation ? 1 : 0,
		schedule_enabled: form.schedule_enabled ? 1 : 0,
		schedule_frequency: form.schedule_frequency,
		schedule_time: form.schedule_time,
		schedule_day: form.schedule_day,
		stepsJson: JSON.stringify(cleanSteps(form.steps)),
		merged_prompt: form.merged_prompt,
	};
}

let bypassGuard = false;

async function init() {
	bypassGuard = false;
	loadError.value = "";
	fieldErrors.macro_name = "";
	stepErrors.value = {};
	docmeta.value = null;
	if (props.isNew) {
		macro.value = null;
		seed({ enabled: 1, stop_on_error: 1 });
		// "Save as macro" hand-off from chat (§6.3): applied AFTER the snapshot
		// so the prefilled draft counts as unsaved work (dirty guard protects it)
		const pre = takeMacroPrefill();
		if (pre) {
			if (pre.macro_name) form.macro_name = pre.macro_name;
			if (pre.description) form.description = pre.description;
			if (pre.steps && pre.steps.length) form.steps = mapSteps(pre.steps);
		}
		return;
	}
	if (!props.id) return;
	loading.value = true;
	try {
		const full = await api.getMacro(props.id);
		macro.value = full;
		seed(full);
		docmeta.value = useDocmeta(DOCTYPE, props.id); // auto-loads on creation
	} catch (e) {
		loadError.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

watch(() => [props.id, props.isNew], init, { immediate: true });

// False when the reload failed: local state is kept, and the caller says so.
async function reloadMacro() {
	try {
		const full = await api.getMacro(props.id);
		macro.value = full;
		seed(full);
		return true;
	} catch (e) {
		return false;
	}
}

// ── save (round-2 MacrosView semantics, ported) ──────────────────────────────
async function save() {
	if (saving.value || !dirty.value) return;
	// Everything that is wrong is marked AT ONCE, each under its own field, with
	// one toast naming them (never a toast per attempt).
	const name = (form.macro_name || "").trim();
	fieldErrors.macro_name = name ? "" : "Give the macro a name.";
	stepErrors.value = promptlessSteps(form.steps);
	const promptless = Object.keys(stepErrors.value).map((i) => Number(i) + 1);
	const problems = [
		fieldErrors.macro_name,
		promptless.length &&
			`Step${promptless.length === 1 ? "" : "s"} ${promptless.join(", ")} ` +
				`need${promptless.length === 1 ? "s" : ""} a prompt.`,
	].filter(Boolean);
	if (problems.length) {
		toast.error(problems.join(" "));
		return;
	}
	const steps = cleanSteps(form.steps);
	if (!steps.length) {
		toast.error("Add at least one step with a prompt.");
		return;
	}
	saving.value = true;
	try {
		const payload = {
			macro_name: name,
			description: form.description || "",
			steps,
			enabled: form.enabled ? 1 : 0,
			stop_on_error: form.stop_on_error ? 1 : 0,
			skip_confirmation: form.skip_confirmation ? 1 : 0,
			schedule_enabled: form.schedule_enabled ? 1 : 0,
			schedule_frequency: form.schedule_frequency || "daily",
			schedule_time: form.schedule_time || "09:00",
			schedule_weekday: form.schedule_frequency === "weekly" ? form.schedule_day || "" : "",
			schedule_day_of_month:
				form.schedule_frequency === "monthly" ? Number(form.schedule_day) || 0 : 0,
		};
		// Summary handling (update only): an edited summary is explicit intent →
		// send it; a rename-only save keeps the stored one; changed steps with an
		// untouched summary omit it → the backend clears the stale copy and the
		// background re-summarize regenerates it.
		const stepsTouched = JSON.stringify(steps) !== snapshot.value.stepsJson;
		const mergedTouched = (form.merged_prompt || "") !== (snapshot.value.merged_prompt || "");
		let sentMerged = "";
		let savedName = props.isNew ? "" : props.id;
		if (props.isNew) {
			const r = (await api.createMacro(payload)) || {};
			savedName = (r.data && r.data.name) || "";
		} else {
			const upd = { name: props.id, ...payload };
			if (mergedTouched || !stepsTouched) {
				upd.merged_prompt = (form.merged_prompt || "").trim();
				sentMerged = upd.merged_prompt;
			}
			await api.updateMacro(upd);
			// The server has these edits from here on, whatever the reload below
			// does. The form is clean as of now: left to the reload alone, one
			// failed request kept it "dirty", with Run off behind "Save your changes
			// first" and a second Save judging the steps against a stale snapshot.
			// A summary this save omitted was cleared by the server (see above), so
			// it goes from the form too: kept, the next rename-only save would send
			// the stale text back as if the owner had written it.
			if (!("merged_prompt" in upd)) form.merged_prompt = "";
			snapshot.value = formSnapshot();
		}
		// Re-summarize only when the sequence actually changed (or has no summary
		// yet) - a rename shouldn't burn an LLM turn.
		const needsSummary = steps.length >= 2 && (stepsTouched || props.isNew || !sentMerged);
		if (savedName && needsSummary) {
			try {
				await api.summarizeMacro(savedName);
				mergeStatus.value = "pending"; // Run waits for it, reload or no reload
				toast.create({
					message:
						"Summarizing in the background - Run unlocks when the summary is ready.",
					type: "info",
				});
			} catch (e) {
				// macro is saved either way; without a summary the steps run
			}
		}
		toast.success("Saved");
		if (props.isNew) {
			bypassGuard = true;
			if (savedName) router.replace("/macros/" + savedName);
		} else if (!(await reloadMacro())) {
			// The reload picks up merge_status / the cleared summary / next_run_at.
			// Without it the save still stands; only those may be out of date.
			toast.create({
				message:
					"Saved, but this page could not be refreshed. Reload it to see the latest summary and next run.",
				type: "warning",
			});
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

// ── run / re-summarize / delete ──────────────────────────────────────────────
async function run() {
	// The server runs the SAVED macro, so with unsaved edits Run would start
	// something other than what is on screen (and the hand-off to the chat would
	// then ask to discard those edits, the run already under way).
	if (running.value || mergePending.value || dirty.value) return;
	running.value = true;
	try {
		const res = await api.runMacro(props.id);
		const data = (res && res.data) || res || {};
		toast.success("Macro started");
		// hand off to the chat - the live macro banner is ChatView's machinery
		if (data.conversation) router.push("/c/" + data.conversation);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		running.value = false;
	}
}

async function resummarize() {
	try {
		await api.summarizeMacro(props.id);
		mergeStatus.value = "pending";
		toast.create({
			message: "Summarizing in the background - Run unlocks when the summary is ready.",
			type: "info",
		});
	} catch (e) {
		toast.error(errHtml(e));
	}
}

function confirmDelete() {
	// A macro that is running is stopped by the delete (server side, before the
	// row goes). Said here when this page knows of a live run: the last run it
	// loaded, kept current by `macro:done`.
	const running = deleteWarning([lastRun.value]);
	confirmDialog({
		title: "Delete macro?",
		// ConfirmDialog renders `message` as HTML (v-html); the name is the owner's
		// free text, so it goes in escaped.
		message: `Delete “${escapeHtml(form.macro_name || props.id)}”? ${
			running ? `${running} ` : ""
		}Its run history is deleted too. This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				const res = await api.deleteMacro(props.id);
				bypassGuard = true;
				hideDialog();
				toast.success(`Macro deleted${stoppedRunsNote(res)}`);
				router.push({ name: "MacrosList" });
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

// ── live merge updates for THIS macro (badge + Run gate + summary body) ──────
function onEvent(p) {
	if (!p || props.isNew || p.macro !== props.id) return;
	// A run of this macro just ended: the line about the last run is out of date.
	if (p.kind === "macro:done") return refreshLastRun();
	if (p.kind !== "macro:merged") return;
	refreshMergeFields();
	if (p.status === "ready") {
		toast.success("Summary ready - this macro now runs as one prompt.");
	} else {
		toast.create({
			message: "Couldn't summarize - the steps run as a sequence.",
			type: "info",
		});
	}
}

// Only the last-run summary: the form may hold unsaved edits, so nothing else is
// re-seeded.
async function refreshLastRun() {
	const id = props.id;
	try {
		const full = await api.getMacro(id);
		// The user may have opened another macro while this was in flight.
		if (id === props.id) lastRun.value = full.last_run || null;
	} catch (e) {
		// keep what is shown; the next load corrects it
	}
}

async function refreshMergeFields() {
	if (!snapshot.value) return; // initial load still in flight; it lands fresh anyway
	try {
		const full = await api.getMacro(props.id);
		mergeStatus.value = full.merge_status || "";
		// don't clobber an in-progress manual edit of the summary
		if ((form.merged_prompt || "") === (snapshot.value.merged_prompt || "")) {
			form.merged_prompt = full.merged_prompt || "";
		}
		snapshot.value.merged_prompt = full.merged_prompt || "";
	} catch (e) {
		// best-effort; the next full load reconciles
	}
}

onMounted(() => {
	socket && socket.on && socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	socket && socket.off && socket.off("jarvis:event", onEvent);
});

// ── dirty guard (D21) ────────────────────────────────────────────────────────
onBeforeRouteLeave((to, from, next) => {
	if (bypassGuard || !dirty.value) return next();
	let decided = false;
	confirmDialog({
		title: "Discard unsaved changes?",
		message: "Your edits to this macro will be lost.",
		onConfirm: ({ hideDialog }) => {
			decided = true;
			hideDialog();
			next();
		},
		onCancel: () => {
			if (!decided) next(false);
		},
	});
});
</script>
