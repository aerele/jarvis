<template>
	<!-- /triggers/new for a non-admin: the same friendly no-access state the
	     Triggers tab shell uses, instead of a dead fully-disabled form. Gated
	     on capsLoaded so admins never see it flash while the probe resolves. -->
	<div
		v-if="newDenied"
		class="flex h-full flex-col items-center justify-center gap-3 px-8 text-center"
	>
		<FeatherIcon name="git-branch" class="size-7.5 text-ink-gray-5" />
		<div class="flex flex-col items-center gap-1">
			<span class="text-lg font-medium text-ink-gray-8">No access to create triggers</span>
			<span class="text-p-base text-ink-gray-6">
				Creating triggers requires the Jarvis Admin role.
			</span>
		</div>
	</div>

	<DocPage
		v-else
		:breadcrumbs="breadcrumbs"
		:title="pageTitle"
		:status-badge="statusBadge"
		:dirty="dirty"
		:loading="loading"
		:error="loadError"
	>
		<template #actions>
			<Dropdown v-if="!isNew && caps.can_manage" :options="overflowOptions">
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
			<Button
				v-if="caps.can_manage"
				variant="solid"
				label="Save"
				:disabled="!dirty || scriptSaveBlocked"
				:loading="saving"
				@click="save"
			/>
		</template>

		<template #main>
			<!-- 1. Trigger -->
			<DocSection label="Trigger">
				<div class="space-y-4">
					<div>
						<FormControl
							type="text"
							label="Name"
							required
							placeholder="e.g. Big invoice warning"
							:modelValue="form.trigger_name"
							:disabled="readOnly || saving"
							:aria-invalid="fieldErrors.trigger_name ? 'true' : undefined"
							@update:modelValue="(v) => (form.trigger_name = v)"
						/>
						<ErrorMessage :message="fieldErrors.trigger_name" class="mt-2" />
					</div>
					<FormControl
						type="textarea"
						label="Description"
						:rows="2"
						placeholder="What does this trigger do?"
						:modelValue="form.description"
						:disabled="readOnly || saving"
						@update:modelValue="(v) => (form.description = v)"
					/>
					<Switch
						v-model="form.enabled"
						label="Enabled"
						description="Off = the trigger stays saved but never fires."
						:disabled="readOnly || saving"
					/>
					<div>
						<!-- hand-rolled label (Autocomplete has no label prop): mirror
						     FormLabel's required recipe - red asterisk + sr-only text -->
						<label class="mb-1.5 block text-xs text-ink-gray-5">
							DocType
							<span class="select-none text-ink-red-3" aria-hidden="true">*</span>
							<span class="sr-only">(required)</span>
						</label>
						<!-- Autocomplete (0.1.278) has no disabled prop - read-only /
						     mid-save states swap in a disabled FormControl instead -->
						<Autocomplete
							v-if="!readOnly && !saving"
							:options="doctypeOptions"
							:modelValue="doctypeValue"
							placeholder="Pick a DocType…"
							@update:query="onDoctypeQuery"
							@update:modelValue="onDoctypePick"
						/>
						<FormControl
							v-else
							type="text"
							:modelValue="form.target_doctype"
							:disabled="true"
						/>
						<ErrorMessage :message="fieldErrors.target_doctype" class="mt-2" />
					</div>
					<div>
						<FormControl
							type="select"
							label="Event"
							required
							:options="docEventOptions"
							:modelValue="form.doc_event"
							:disabled="readOnly || saving"
							:aria-invalid="fieldErrors.doc_event ? 'true' : undefined"
							:description="
								form.action_type === 'LLM'
									? 'LLM actions support a reduced set of events.'
									: ''
							"
							@update:modelValue="(v) => (form.doc_event = v)"
						/>
						<ErrorMessage :message="fieldErrors.doc_event" class="mt-2" />
					</div>
				</div>
			</DocSection>

			<!-- 2. Condition -->
			<DocSection label="Condition">
				<div class="space-y-2">
					<FormControl
						type="textarea"
						:rows="4"
						class="font-mono"
						placeholder='(doc.grand_total or 0) > 100000 and doc.status == "Paid"'
						:modelValue="form.condition"
						:disabled="readOnly || saving"
						@update:modelValue="(v) => (form.condition = v)"
					/>
					<div class="text-p-sm text-ink-gray-5">
						Optional webhook-style Python expression over
						<span class="font-mono">doc</span> - the trigger fires only when it's true.
						Examples:
						<span class="font-mono">(doc.grand_total or 0) &gt; 100000</span> ·
						<span class="font-mono">doc.status == "Overdue"</span>. Leave empty to fire
						on every event.
					</div>
					<Button
						variant="ghost"
						label="Test condition"
						iconLeft="check-circle"
						@click="openTest"
					/>
				</div>
			</DocSection>

			<!-- 3. Action -->
			<DocSection label="Action">
				<div class="space-y-4">
					<!-- segmented mode switch (option-chip idiom - TabButtons isn't
					     used anywhere in this app yet, so stay with proven pieces) -->
					<div class="flex flex-wrap gap-2">
						<Button
							v-for="t in ACTION_TYPES"
							:key="t"
							:label="t"
							:variant="form.action_type === t ? 'solid' : 'subtle'"
							:disabled="readOnly || saving"
							@click="setActionType(t)"
						/>
					</div>

					<template v-if="form.action_type === 'Script'">
						<!-- scripts disabled on this bench: warning banner + Save gate -->
						<div
							v-if="!caps.scripts_enabled"
							class="flex items-start gap-2 rounded-md bg-surface-amber-2 p-2"
						>
							<FeatherIcon
								name="alert-triangle"
								class="mt-0.5 size-4 shrink-0 text-ink-amber-3"
							/>
							<span class="text-sm font-medium text-ink-gray-8">
								Server scripts are disabled on this bench — deterministic actions
								can't be saved. LLM actions still work.
							</span>
						</div>
						<FormControl
							type="textarea"
							label="Script"
							:rows="12"
							class="font-mono [&_textarea]:min-h-[16rem]"
							placeholder="# Python (Server Script sandbox) - runs when the trigger fires"
							:modelValue="form.script_body"
							:disabled="readOnly || saving || !caps.scripts_enabled"
							@update:modelValue="(v) => (form.script_body = v)"
						/>
					</template>

					<template v-else>
						<FormControl
							type="textarea"
							label="Instruction"
							:rows="5"
							placeholder="e.g. Summarize the document and warn me in chat if anything looks off"
							:modelValue="form.llm_instruction"
							:disabled="readOnly || saving"
							@update:modelValue="(v) => (form.llm_instruction = v)"
						/>
						<FormControl
							type="number"
							label="Daily cap"
							description="Max LLM evaluations per day for this trigger"
							:modelValue="form.llm_daily_cap"
							:disabled="readOnly || saving"
							@update:modelValue="(v) => (form.llm_daily_cap = v)"
						/>
					</template>
				</div>
			</DocSection>

			<!-- 4. Recent activity (existing triggers only) -->
			<DocSection v-if="!isNew" label="Recent activity">
				<div v-if="recentRows.length" class="flex flex-col">
					<button
						v-for="(r, i) in recentRows"
						:key="r.name"
						class="flex w-full items-center gap-3 px-1 py-2 text-left hover:bg-surface-gray-1"
						:class="i ? 'border-t' : ''"
						@click="openActivityRow(r)"
					>
						<Badge
							class="shrink-0"
							variant="subtle"
							:theme="STATUS_THEME[r.status] || 'gray'"
							:label="r.status || '-'"
						/>
						<span class="min-w-0 flex-1 truncate text-base text-ink-gray-7">
							{{ r.summary || r.target_doctype + " · " + r.target_docname }}
						</span>
						<Tooltip :text="exactDate(r.creation)">
							<span class="shrink-0 whitespace-nowrap text-sm text-ink-gray-5">
								{{ timeAgo(r.creation) }}
							</span>
						</Tooltip>
					</button>
					<div class="pt-2">
						<router-link
							:to="{
								name: 'TriggersPage',
								query: { trigger: props.id },
								hash: '#activity',
							}"
							class="text-sm text-ink-blue-link hover:underline"
						>
							View all
						</router-link>
					</div>
				</div>
				<div v-else class="text-sm text-ink-gray-5">
					No activity yet - runs appear here when the trigger fires.
				</div>
			</DocSection>
		</template>
	</DocPage>

	<!-- Test-condition dialog -->
	<Dialog v-model="testOpen" :options="{ title: 'Test condition', size: 'lg' }">
		<template #body-content>
			<div class="space-y-4">
				<div class="text-p-base text-ink-gray-7">
					Validates the condition against
					<span class="font-medium">{{
						form.target_doctype || "the chosen DocType"
					}}</span
					>; give a document name to also check whether it would fire.
				</div>
				<FormControl
					type="text"
					label="Document name (optional)"
					placeholder="e.g. ACC-SINV-2026-00042"
					:modelValue="testDocname"
					:disabled="testing"
					@update:modelValue="(v) => (testDocname = v)"
				/>
				<div v-if="testResult" class="flex flex-col gap-1.5">
					<div
						v-if="testResult.valid"
						class="flex items-center gap-2 text-sm text-ink-green-3"
					>
						<FeatherIcon name="check-circle" class="size-4 shrink-0" />
						Condition is valid.
					</div>
					<div v-else class="flex items-start gap-2 text-sm text-ink-red-4" role="alert">
						<FeatherIcon name="alert-circle" class="mt-0.5 size-4 shrink-0" />
						<span>{{ testResult.error || "Condition is not valid." }}</span>
					</div>
					<div
						v-if="testResult.valid && testResult.would_fire != null"
						class="flex items-center gap-2 text-sm"
						:class="testResult.would_fire ? 'text-ink-green-3' : 'text-ink-gray-6'"
					>
						<FeatherIcon
							:name="testResult.would_fire ? 'zap' : 'zap-off'"
							class="size-4 shrink-0"
						/>
						{{
							testResult.would_fire
								? "Would fire for this document."
								: "Would NOT fire for this document."
						}}
					</div>
				</div>
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Close" @click="testOpen = false" />
				<Button
					variant="solid"
					label="Test"
					:loading="testing"
					:disabled="!form.target_doctype"
					@click="runTest"
				/>
			</div>
		</template>
	</Dialog>

	<ActivityDetailDialog v-model="activityOpen" :row="activityRow" :caps="caps" />
</template>

<script setup>
// Trigger detail + create page: /triggers/:id and /triggers/new share this
// component (isNew prop), copied from MacroDetail's DocPage skeleton -
// explicit Save (dirty-gated snapshot compare), "..." Dropdown with
// Enable/Disable + Delete (confirmDialog), route-leave dirty guard. Sections:
// Trigger (name/description/enabled/DocType autocomplete/event select) ·
// Condition (mono textarea + webhook-style examples + Test-condition dialog
// via test_trigger_condition) · Action (Script/LLM segmented control; Script
// gated on caps.scripts_enabled) · Recent activity (last 10 rows for this
// trigger, dialog detail, View-all deep link that pre-filters #activity).
// update_trigger receives ONLY the changed fields; create_trigger the full
// known set. Non-admins read: every control disabled, no Save/overflow.
import { ref, reactive, computed, watch, onMounted } from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Autocomplete,
	Badge,
	Button,
	Dialog,
	Dropdown,
	ErrorMessage,
	FeatherIcon,
	FormControl,
	Switch,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import DocPage from "@/components/doc/DocPage.vue";
import DocSection from "@/components/doc/DocSection.vue";
import ActivityDetailDialog from "./ActivityDetailDialog.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import { searchLink } from "@/api";
import * as apiTriggers from "@/api/triggers";
import { errMessage as errMsg } from "@/lib/errors";

const props = defineProps({
	id: { type: String, default: "" },
	isNew: { type: Boolean, default: false },
});

const router = useRouter();

const ACTION_TYPES = ["Script", "LLM"];
const STATUS_THEME = { Success: "green", Failed: "red", Blocked: "orange", Skipped: "gray" };

// ── caps (deep-linkable page → its own probe) ────────────────────────────────
const caps = ref({
	can_manage: false,
	scripts_enabled: false,
	stt_enabled: false,
	events: [],
	llm_events: [],
});
const readOnly = computed(() => !caps.value.can_manage);
// /triggers/new is pointless read-only: once the probe settles, a non-admin
// gets the shell's no-access state instead of a fully-disabled form. Gating
// on capsLoaded keeps it from flashing at admins while the probe is in flight.
const capsLoaded = ref(false);
const newDenied = computed(() => props.isNew && capsLoaded.value && !caps.value.can_manage);

// ── state ─────────────────────────────────────────────────────────────────────
const loading = ref(false);
const loadError = ref("");
const saving = ref(false);

const form = reactive({
	trigger_name: "",
	description: "",
	enabled: true,
	target_doctype: "",
	doc_event: "",
	condition: "",
	action_type: "LLM",
	script_body: "",
	llm_instruction: "",
	llm_daily_cap: 25,
});
// Saved-state copy for the dirty compare - a ref, per MacroDetail's lesson
// (the computed must track it while the initial load is in flight).
const snapshot = ref(null);

// Inline required-field errors (design.md: ErrorMessage under the field, no
// error prop on FormControl). Set all at once by save(); each clears itself
// the moment its field gets a value.
const fieldErrors = reactive({ trigger_name: "", target_doctype: "", doc_event: "" });
watch(
	() => [form.trigger_name, form.target_doctype, form.doc_event],
	() => {
		if (String(form.trigger_name || "").trim()) fieldErrors.trigger_name = "";
		if (form.target_doctype) fieldErrors.target_doctype = "";
		if (form.doc_event) fieldErrors.doc_event = "";
	}
);

const dirty = computed(() => {
	const snap = snapshot.value;
	if (!snap || loading.value) return false;
	return FIELDS.some((f) => normalized(f) !== snap[f]);
});

// a Script action can't be saved while server scripts are off (the banner
// explains; LLM saves keep working)
const scriptSaveBlocked = computed(
	() => form.action_type === "Script" && !caps.value.scripts_enabled
);

const pageTitle = computed(() =>
	props.isNew ? form.trigger_name || "New trigger" : form.trigger_name || props.id
);
const breadcrumbs = computed(() => [
	{ label: "Triggers", route: { name: "TriggersPage" } },
	props.isNew
		? { label: "New trigger", route: { name: "TriggerNew" } }
		: { label: pageTitle.value, route: { name: "TriggerDetail", params: { id: props.id } } },
]);
const statusBadge = computed(() => {
	if (props.isNew || !snapshot.value) return null;
	return form.enabled
		? { label: "Enabled", theme: "green" }
		: { label: "Disabled", theme: "gray" };
});

const overflowOptions = computed(() => [
	{
		label: form.enabled ? "Disable" : "Enable",
		icon: form.enabled ? "pause" : "play",
		onClick: toggleEnabled,
	},
	{ label: "Delete", icon: "trash-2", onClick: confirmDelete },
]);

// ── field normalization (one list drives dirty + changed-payload) ────────────
const FIELDS = [
	"trigger_name",
	"description",
	"enabled",
	"target_doctype",
	"doc_event",
	"condition",
	"action_type",
	"script_body",
	"llm_instruction",
	"llm_daily_cap",
];
function normalized(f) {
	if (f === "enabled") return form.enabled ? 1 : 0;
	if (f === "llm_daily_cap") return Number(form.llm_daily_cap) || 0;
	return String(form[f] == null ? "" : form[f]);
}

// ── doc_event options (LLM restricts to caps.llm_events) ─────────────────────
const docEventOptions = computed(() => {
	let events = caps.value.events || [];
	if (form.action_type === "LLM") {
		const allowed = new Set(caps.value.llm_events || []);
		events = events.filter((e) => allowed.has(e.value));
	}
	const opts = events.map((e) => ({ label: e.label, value: e.value }));
	// a blank leading option keeps the control honest while doc_event is unset
	// (a native select with no matching option would LOOK like the first choice)
	if (!form.doc_event) opts.unshift({ label: "Pick an event…", value: "" });
	return opts;
});

function setActionType(t) {
	if (form.action_type === t) return;
	form.action_type = t;
	// an event outside the LLM whitelist can't stay selected under LLM
	if (form.doc_event && !docEventOptions.value.some((o) => o.value === form.doc_event)) {
		const first = docEventOptions.value.find((o) => o.value);
		form.doc_event = first ? first.value : "";
	}
}

// ── DocType autocomplete (frappe.desk.search.search_link, the in-repo
//    doctype-picker path ChatView's mentions already use) ─────────────────────
const doctypeOptions = ref([]);
const doctypeValue = computed(() =>
	form.target_doctype ? { label: form.target_doctype, value: form.target_doctype } : null
);
let dtTimer = null;
let dtSeq = 0;
function onDoctypeQuery(q) {
	clearTimeout(dtTimer);
	dtTimer = setTimeout(() => loadDoctypes(q || ""), 300);
}
async function loadDoctypes(q) {
	const seq = ++dtSeq;
	try {
		const r = await searchLink("DocType", q);
		if (seq !== dtSeq) return;
		doctypeOptions.value = (r || []).map((x) => ({ label: x.value, value: x.value }));
	} catch (e) {
		if (seq === dtSeq) doctypeOptions.value = [];
	}
}
function onDoctypePick(opt) {
	form.target_doctype = opt && opt.value ? String(opt.value) : "";
}

// ── load / seed ───────────────────────────────────────────────────────────────
function seed(data) {
	form.trigger_name = data.trigger_name || "";
	form.description = data.description || "";
	form.enabled = data.enabled == null ? true : !!data.enabled;
	form.target_doctype = data.target_doctype || "";
	form.doc_event = data.doc_event || "";
	form.condition = data.condition || "";
	form.action_type = data.action_type === "Script" ? "Script" : "LLM";
	form.script_body = data.script_body || "";
	form.llm_instruction = data.llm_instruction || "";
	form.llm_daily_cap = data.llm_daily_cap == null ? 25 : data.llm_daily_cap;
	const snap = {};
	for (const f of FIELDS) snap[f] = normalized(f);
	snapshot.value = snap;
}

let bypassGuard = false;

async function init() {
	bypassGuard = false;
	loadError.value = "";
	if (props.isNew) {
		seed({ enabled: 1, action_type: "LLM", llm_daily_cap: 25 });
		return;
	}
	if (!props.id) return;
	loading.value = true;
	try {
		const full = await apiTriggers.getTrigger(props.id);
		seed(full || {});
		loadRecent();
	} catch (e) {
		loadError.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

watch(() => [props.id, props.isNew], init, { immediate: true });

onMounted(async () => {
	try {
		const fresh = await apiTriggers.getTriggersCaps();
		if (fresh) caps.value = { ...caps.value, ...fresh };
	} catch (e) {
		// keep read-only defaults; a save attempt would fail server-side anyway
	} finally {
		capsLoaded.value = true;
	}
	// seed the DocType picker so the dropdown opens populated
	loadDoctypes("");
});

// ── save (changed/known fields only) ─────────────────────────────────────────
async function save() {
	if (saving.value || !dirty.value || scriptSaveBlocked.value) return;
	// validate every required field AT ONCE: an inline ErrorMessage lands under
	// each empty field, plus one combined toast (never one-toast-per-attempt)
	fieldErrors.trigger_name = String(form.trigger_name || "").trim()
		? ""
		: "Give the trigger a name.";
	fieldErrors.target_doctype = form.target_doctype ? "" : "Pick a DocType.";
	fieldErrors.doc_event = form.doc_event ? "" : "Pick an event.";
	const missing = [
		fieldErrors.trigger_name && "Name",
		fieldErrors.target_doctype && "DocType",
		fieldErrors.doc_event && "Event",
	].filter(Boolean);
	if (missing.length) {
		toast.error(
			`Fill in the required field${missing.length === 1 ? "" : "s"}: ${missing.join(", ")}.`
		);
		return;
	}
	saving.value = true;
	try {
		if (props.isNew) {
			const payload = {};
			for (const f of FIELDS) payload[f] = normalized(f);
			payload.trigger_name = payload.trigger_name.trim();
			const full = (await apiTriggers.createTrigger(payload)) || {};
			toast.success("Trigger created");
			bypassGuard = true;
			if (full.name) router.replace("/triggers/" + full.name);
			else router.push({ name: "TriggersPage" });
		} else {
			const payload = {};
			for (const f of FIELDS) {
				const v = normalized(f);
				if (v !== snapshot.value[f])
					payload[f] = f === "trigger_name" ? String(v).trim() : v;
			}
			const full = (await apiTriggers.updateTrigger(props.id, payload)) || null;
			// contract: update returns the full detail; reconcile defensively
			if (full && full.name) seed(full);
			else seed((await apiTriggers.getTrigger(props.id)) || {});
			toast.success("Saved");
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		saving.value = false;
	}
}

// ── enable/disable + delete ──────────────────────────────────────────────────
async function toggleEnabled() {
	const next = form.enabled ? 0 : 1;
	try {
		await apiTriggers.setTriggerEnabled(props.id, next);
		form.enabled = !!next;
		if (snapshot.value) snapshot.value.enabled = next;
		toast.success(next ? "Trigger enabled" : "Trigger disabled");
	} catch (e) {
		toast.error(errMsg(e));
	}
}

function confirmDelete() {
	confirmDialog({
		title: "Delete trigger?",
		message: `Delete “${form.trigger_name || props.id}”? This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await apiTriggers.deleteTrigger(props.id);
				bypassGuard = true;
				hideDialog();
				toast.success("Trigger deleted");
				router.push({ name: "TriggersPage" });
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}

// ── test-condition dialog ────────────────────────────────────────────────────
const testOpen = ref(false);
const testDocname = ref("");
const testing = ref(false);
const testResult = ref(null);

function openTest() {
	testResult.value = null;
	testOpen.value = true;
}

async function runTest() {
	if (testing.value || !form.target_doctype) return;
	testing.value = true;
	testResult.value = null;
	try {
		testResult.value =
			(await apiTriggers.testTriggerCondition(
				form.target_doctype,
				form.condition || "",
				testDocname.value.trim()
			)) || null;
	} catch (e) {
		testResult.value = { valid: false, error: errMsg(e) };
	} finally {
		testing.value = false;
	}
}

// ── recent activity (existing triggers only) ─────────────────────────────────
const recentRows = ref([]);
async function loadRecent() {
	try {
		const res =
			(await apiTriggers.listActivityPage({
				filters: { trigger: props.id },
				sort_field: "creation",
				sort_dir: "desc",
				start: 0,
				page_length: 10,
			})) || {};
		recentRows.value = res.rows || [];
	} catch (e) {
		// best-effort block; the Activity tab is the full surface
	}
}

const activityOpen = ref(false);
const activityRow = ref(null);
function openActivityRow(r) {
	activityRow.value = r;
	activityOpen.value = true;
}

// ── dirty guard (MacroDetail D21) ────────────────────────────────────────────
onBeforeRouteLeave((to, from, next) => {
	if (bypassGuard || !dirty.value) return next();
	let decided = false;
	confirmDialog({
		title: "Discard unsaved changes?",
		message: "Your edits to this trigger will be lost.",
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
