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
				:disabled="mergePending || running"
				:tooltip="
					mergePending
						? 'Summarizing… - Run unlocks when the summary is ready'
						: 'Run this macro now'
				"
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
			<DocSection label="Details">
				<div class="space-y-4">
					<FormControl
						type="text"
						label="Name"
						placeholder="e.g. Monthly close"
						:modelValue="form.macro_name"
						:disabled="saving"
						@update:modelValue="(v) => (form.macro_name = v)"
					/>
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
						description="Off = saved as a draft - it won't run and stays out of the chat run menu."
						:disabled="saving"
					/>
					<Switch
						v-model="form.stop_on_error"
						label="Stop on error"
						description="Stop the chain if a step fails - otherwise it keeps going after an error."
						:disabled="saving"
					/>
				</div>
			</DocSection>

			<DocSection label="Schedule">
				<div class="space-y-4">
					<Switch
						v-model="form.schedule_enabled"
						label="Run on a schedule"
						:description="`${agentName} runs this macro automatically.`"
						:disabled="saving"
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
					</div>
					<div
						v-if="!isNew && form.schedule_enabled && nextRunAt"
						class="text-sm text-ink-gray-5"
					>
						Next run: {{ nextRunAt }}
					</div>
				</div>
			</DocSection>

			<DocSection label="Steps">
				<StepsBuilder v-model="form.steps" :disabled="saving" />
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
	shallowRef,
	inject,
	onMounted,
	onBeforeUnmount,
} from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Button,
	Badge,
	Dropdown,
	FormControl,
	Switch,
	TimePicker,
	toast,
	confirmDialog,
} from "frappe-ui";
import DocPage from "@/components/doc/DocPage.vue";
import DocSection from "@/components/doc/DocSection.vue";
import DocMetaPanel from "@/components/doc/DocMetaPanel.vue";
import CommentsSection from "@/components/doc/CommentsSection.vue";
import { useDocmeta } from "@/composables/useDocmeta";
import StepsBuilder from "./StepsBuilder.vue";
import { takeMacroPrefill } from "@/composables/macroPrefill";
import { exactDate } from "@/utils/datetime";
import * as api from "@/api";
import { agentName } from "@/branding";

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

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── state ────────────────────────────────────────────────────────────────────
const macro = ref(null); // last server copy (null while new)
const loading = ref(false);
const loadError = ref("");
const saving = ref(false);
const running = ref(false);
const mergeStatus = ref(""); // '' | 'pending' | 'ready' | 'failed'
const nextRunRaw = ref("");

const form = reactive({
	macro_name: "",
	description: "",
	enabled: true,
	stop_on_error: true,
	schedule_enabled: false,
	schedule_frequency: "daily",
	schedule_time: "09:00",
	steps: [],
	merged_prompt: "",
});
// Saved-state copy for the dirty compare (set by seed). MUST be a ref: on
// /macros/:id the dirty computed first evaluates while the load is in flight,
// and with a plain variable the `!snapshot` short-circuit would track zero
// reactive deps - the computed caches false and never re-runs, so Save never
// enables on existing macros.
const snapshot = ref(null);

const docmeta = shallowRef(null); // useDocmeta instance (persisted macros only)

const mergePending = computed(() => mergeStatus.value === "pending");
const stepsWithPrompt = computed(() => form.steps.filter((s) => (s.prompt || "").trim()).length);

const dirty = computed(() => {
	const snap = snapshot.value;
	if (!snap || loading.value) return false;
	return (
		(form.macro_name || "") !== snap.macro_name ||
		(form.description || "") !== snap.description ||
		(form.enabled ? 1 : 0) !== snap.enabled ||
		(form.stop_on_error ? 1 : 0) !== snap.stop_on_error ||
		(form.schedule_enabled ? 1 : 0) !== snap.schedule_enabled ||
		(form.schedule_frequency || "daily") !== snap.schedule_frequency ||
		(form.schedule_time || "") !== snap.schedule_time ||
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

function cleanSteps(steps) {
	return (steps || [])
		.map((s) => ({
			label: (s.label || "").trim(),
			prompt: (s.prompt || "").trim(),
			skills: Array.isArray(s.skills) ? s.skills : [],
			...(s.model_override ? { model_override: s.model_override } : {}),
			...(s.thinking_override ? { thinking_override: s.thinking_override } : {}),
		}))
		.filter((s) => s.prompt);
}

function toHHMM(t) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}

// ── load / init (re-runs when /macros/new saves and replaces to /macros/:id) ─
function seed(data) {
	form.macro_name = data.macro_name || "";
	form.description = data.description || "";
	form.enabled = data.enabled == null ? true : !!data.enabled;
	form.stop_on_error = !!data.stop_on_error;
	form.schedule_enabled = !!data.schedule_enabled;
	form.schedule_frequency = data.schedule_frequency || "daily";
	form.schedule_time = toHHMM(data.schedule_time) || "09:00";
	form.steps = mapSteps(data.steps);
	if (!form.steps.length) form.steps = [{ label: "", prompt: "", skills: [] }];
	form.merged_prompt = data.merged_prompt || "";
	mergeStatus.value = data.merge_status || "";
	nextRunRaw.value = data.next_run_at || "";
	snapshot.value = {
		macro_name: form.macro_name,
		description: form.description,
		enabled: form.enabled ? 1 : 0,
		stop_on_error: form.stop_on_error ? 1 : 0,
		schedule_enabled: form.schedule_enabled ? 1 : 0,
		schedule_frequency: form.schedule_frequency,
		schedule_time: form.schedule_time,
		stepsJson: JSON.stringify(cleanSteps(form.steps)),
		merged_prompt: form.merged_prompt,
	};
}

let bypassGuard = false;

async function init() {
	bypassGuard = false;
	loadError.value = "";
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

async function reloadMacro() {
	try {
		const full = await api.getMacro(props.id);
		macro.value = full;
		seed(full);
	} catch (e) {
		// keep local state; next navigation reloads
	}
}

// ── save (round-2 MacrosView semantics, ported) ──────────────────────────────
async function save() {
	if (saving.value || !dirty.value) return;
	const name = (form.macro_name || "").trim();
	if (!name) {
		toast.error("Give the macro a name.");
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
			schedule_enabled: form.schedule_enabled ? 1 : 0,
			schedule_frequency: form.schedule_frequency || "daily",
			schedule_time: form.schedule_time || "09:00",
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
		}
		// Re-summarize only when the sequence actually changed (or has no summary
		// yet) - a rename shouldn't burn an LLM turn.
		const needsSummary = steps.length >= 2 && (stepsTouched || props.isNew || !sentMerged);
		if (savedName && needsSummary) {
			try {
				await api.summarizeMacro(savedName);
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
		} else {
			await reloadMacro(); // picks up merge_status / cleared summary / next_run_at
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		saving.value = false;
	}
}

// ── run / re-summarize / delete ──────────────────────────────────────────────
async function run() {
	if (running.value || mergePending.value) return;
	running.value = true;
	try {
		const res = await api.runMacro(props.id);
		const data = (res && res.data) || res || {};
		toast.success("Macro started");
		// hand off to the chat - the live macro banner is ChatView's machinery
		if (data.conversation) router.push("/c/" + data.conversation);
	} catch (e) {
		toast.error(errMsg(e));
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
		toast.error(errMsg(e));
	}
}

function confirmDelete() {
	confirmDialog({
		title: "Delete macro?",
		message: `Delete “${
			form.macro_name || props.id
		}”? Its run history is deleted too. This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await api.deleteMacro(props.id);
				bypassGuard = true;
				hideDialog();
				toast.success("Macro deleted");
				router.push({ name: "MacrosList" });
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}

// ── live merge updates for THIS macro (badge + Run gate + summary body) ──────
function onEvent(p) {
	if (!p || p.kind !== "macro:merged" || props.isNew || p.macro !== props.id) return;
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
