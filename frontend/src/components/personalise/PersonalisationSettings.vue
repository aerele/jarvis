<template>
	<Dialog
		:modelValue="open"
		:options="{ size: '5xl' }"
		@update:modelValue="(v) => emit('update:open', v)"
		@after-leave="onClosed"
	>
		<template #body>
			<div class="flex h-[calc(100vh_-_8rem)] flex-col">
				<!-- own header row: #body is a full override (FilePreview.vue's idiom),
				     so the close button lives here instead of Dialog's default title bar -->
				<div class="flex h-[45px] shrink-0 items-center justify-between border-b px-5">
					<span class="text-lg font-semibold text-ink-gray-9"
						>Personalisation Settings</span
					>
					<Button variant="ghost" icon="x" :tooltip="'Close'" @click="close" />
				</div>

				<div class="flex min-h-0 flex-1">
					<!-- ─────────── left nav (CRM Settings.vue idiom) ─────────── -->
					<div class="w-56 shrink-0 overflow-y-auto border-r p-4">
						<div class="text-xs-medium uppercase tracking-wide text-ink-gray-5">
							Settings
						</div>
						<nav class="mt-2 flex flex-col gap-0.5">
							<button
								v-for="s in SECTIONS"
								:key="s.value"
								type="button"
								class="rounded px-2.5 py-1.5 text-left text-sm"
								:class="
									section === s.value
										? 'bg-surface-elevation-3 text-ink-gray-9 shadow-sm'
										: 'text-ink-gray-6 hover:bg-surface-gray-3'
								"
								@click="section = s.value"
							>
								{{ s.label }}
							</button>
						</nav>
					</div>

					<!-- ─────────── right pane ─────────── -->
					<div class="min-w-0 flex-1 overflow-y-auto p-8">
						<!-- ══════════════ Questions (question rules) ══════════════ -->
						<div v-if="section === 'questions'" class="flex flex-col gap-5">
							<div class="flex items-start justify-between gap-3">
								<div>
									<h2 class="text-lg font-semibold text-ink-gray-9">
										Question sources
									</h2>
									<p class="mt-1 text-p-base text-ink-gray-6">
										Admin-authored questions {{ agentName }} asks everyone, a
										role, or one person &mdash; separate from the questions it
										generates on its own from behaviour and chat patterns
										(uncapped, materialized as soon as you save one).
									</p>
								</div>
								<Button
									variant="solid"
									label="New question"
									iconLeft="plus"
									@click="openNew"
								/>
							</div>

							<!-- inline editor card (create or edit; never a separate Dialog) -->
							<div
								v-if="editor.show"
								class="rounded-lg border border-outline-gray-3 bg-surface-gray-1 p-4"
							>
								<div class="flex flex-col gap-3">
									<FormControl
										type="textarea"
										label="Question"
										:rows="2"
										placeholder="e.g. What software do you use to track expenses?"
										:modelValue="editor.question"
										@update:modelValue="(v) => (editor.question = v)"
									/>
									<FormControl
										type="textarea"
										:label="`Context for ${agentName} (optional)`"
										:rows="3"
										:placeholder="`What should ${agentName} share when asking this?`"
										:modelValue="editor.context_md"
										@update:modelValue="(v) => (editor.context_md = v)"
									/>
									<FormControl
										type="select"
										label="Who should be asked?"
										:options="SCOPE_OPTIONS"
										:modelValue="editor.scope"
										@update:modelValue="(v) => (editor.scope = v)"
									/>
									<div
										v-if="editor.scope === 'Role'"
										class="flex flex-col gap-1"
									>
										<span class="block text-xs text-ink-gray-5">Role</span>
										<Autocomplete
											placeholder="Search roles"
											:options="roleOptions"
											:modelValue="editor.target_role"
											@update:modelValue="
												(v) => (editor.target_role = (v && v.value) || '')
											"
										/>
									</div>
									<div
										v-if="editor.scope === 'User'"
										class="flex flex-col gap-1"
									>
										<span class="block text-xs text-ink-gray-5">Person</span>
										<Autocomplete
											placeholder="Search people"
											:options="userOptions"
											:modelValue="editor.target_user"
											@update:modelValue="
												(v) => (editor.target_user = (v && v.value) || '')
											"
										/>
									</div>
									<div class="flex items-center gap-2">
										<Switch v-model="editor.active" />
										<span class="text-sm text-ink-gray-7">Active</span>
									</div>

									<div class="flex items-center justify-end gap-2 border-t pt-3">
										<Button
											label="Cancel"
											:disabled="editor.saving"
											@click="closeEditor"
										/>
										<Button
											variant="solid"
											label="Save"
											:loading="editor.saving"
											:disabled="!canSaveEditor"
											@click="saveEditor"
										/>
									</div>
								</div>
							</div>

							<!-- rule list -->
							<div v-if="rulesLoading" class="grid place-items-center py-10">
								<LoadingIndicator class="size-5 text-ink-gray-5" />
							</div>
							<div
								v-else-if="!rules.length"
								class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-10 text-center"
							>
								<FeatherIcon name="help-circle" class="size-8 text-ink-gray-4" />
								<div class="mt-1 text-base font-medium text-ink-gray-8">
									No configured questions yet
								</div>
								<div class="max-w-sm text-p-base text-ink-gray-6">
									Add what {{ agentName }} should ask every employee, a role, or
									one person.
								</div>
							</div>
							<div v-else class="flex flex-col gap-2">
								<div
									v-for="r in rules"
									:key="r.name"
									class="flex items-center gap-3 rounded-lg border p-3"
								>
									<div class="min-w-0 flex-1">
										<div class="truncate text-sm text-ink-gray-9">
											{{ r.question }}
										</div>
										<Badge
											class="mt-1.5"
											:theme="SCOPE_THEME[r.scope] || 'gray'"
											variant="subtle"
											size="sm"
											:label="scopeLabel(r)"
										/>
									</div>
									<Switch
										:modelValue="!!r.active"
										:disabled="rowActing === r.name"
										@update:modelValue="(v) => toggleActive(r, v)"
									/>
									<Button
										variant="ghost"
										icon="edit-2"
										:tooltip="'Edit'"
										@click="openEdit(r)"
									/>
									<Button
										variant="ghost"
										theme="red"
										icon="trash-2"
										:tooltip="'Delete'"
										@click="confirmDeleteRule(r)"
									/>
								</div>
							</div>
						</div>

						<!-- ══════════════ Limits (caps & toggles) ══════════════ -->
						<div v-else class="flex flex-col gap-5">
							<div>
								<h2 class="text-lg font-semibold text-ink-gray-9">
									Caps &amp; toggles
								</h2>
								<p class="mt-1 text-p-base text-ink-gray-6">
									Control how many behavioural-learning / chat-pattern questions
									{{ agentName }} adds to a person's bank each day. Organisation,
									role, and reviewer follow-up questions are never capped.
								</p>
							</div>

							<div
								class="flex items-start justify-between gap-3 rounded-lg border p-4"
							>
								<div>
									<div class="text-base font-medium text-ink-gray-9">
										Personalisation questions
									</div>
									<div class="mt-0.5 text-sm text-ink-gray-6">
										Turn off to stop {{ agentName }} asking anyone new
										questions. Existing questions stay listed and answerable.
									</div>
								</div>
								<Switch v-model="settings.personalise_enabled" size="md" />
							</div>

							<div
								class="flex items-start justify-between gap-3 rounded-lg border p-4"
							>
								<div>
									<div class="text-base font-medium text-ink-gray-9">
										Learn from chats
									</div>
									<div class="mt-0.5 text-sm text-ink-gray-6">
										Once a day, {{ agentName }} reviews recent chats and drafts
										questions so people can confirm what it should remember.
										Answers become wiki notes and skills.
									</div>
									<div
										v-if="settings.chat_mining_last_run_status"
										class="mt-1.5 text-xs text-ink-gray-5"
									>
										Last run{{ chatMiningLastRunAgo }}:
										{{ settings.chat_mining_last_run_status }}
									</div>
									<Button
										class="mt-2"
										variant="subtle"
										size="sm"
										iconLeft="refresh-cw"
										label="Generate now"
										:loading="generatingNow"
										:disabled="!settings.chat_question_mining_enabled"
										@click="generateNow"
									/>
								</div>
								<Switch
									v-model="settings.chat_question_mining_enabled"
									size="md"
								/>
							</div>

							<FormControl
								type="number"
								label="Max learning / chat-pattern questions per person per day"
								:min="0"
								:modelValue="settings.daily_question_cap"
								@update:modelValue="(v) => (settings.daily_question_cap = v)"
							/>

							<div class="flex justify-end border-t pt-4">
								<Button
									variant="solid"
									label="Save"
									:loading="savingSettings"
									@click="saveSettings"
								/>
							</div>
						</div>
					</div>
				</div>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// PersonalisationSettings — the admin-only settings modal behind the gear on
// the Personalise tab (Wave F4, DESIGN.md §5/§5c-8/§6b). Gated entirely by
// the PARENT (PersonaliseTab only mounts this `v-if="caps.analysis"`, the
// same admin boolean `get_skills_area_caps` returns) — this component itself
// does not re-probe access; it trusts the mount condition, exactly like
// AnalysisTab trusts SkillsPage's tab gate.
//
// CRM Settings.vue idiom (research/design-language.md §4): Dialog size 5xl,
// left nav w-56 + right pane flex-1 overflow-y-auto. Two sections:
//   Questions — CRUD over `Jarvis Personalise Question Rule` rows (admin-
//     authored questions materialized per in-scope user by the PIPELINE
//     agent's sweep — this file only owns the config CRUD, never
//     materialization itself).
//   Limits — the two `Jarvis Settings` fields (personalise_enabled,
//     personalise_daily_question_cap).
//
// v-model:open contract (binding — PersonaliseTab.vue already wired this):
//   <PersonalisationSettings v-if="caps.analysis" v-model:open="settingsOpen" />
// so this component's own prop is named `open`/`update:open`, NOT the bare
// Dialog `modelValue`/`update:modelValue` convention other dialogs in this
// codebase use — the Dialog inside is just an implementation detail wired to
// that same `open` prop.
//
// Role/person targeting:
//   - Role options: `jarvis.chat.personalise_api.list_role_options` — a
//     purpose-built admin endpoint (same admin gate as this screen) returning
//     every enabled desk role. It does NOT reuse the Wiki tab's narrower
//     `get_wiki_caps().manageable_roles`, which returns [] for a Jarvis Admin
//     who is not also a System Manager / Knowledge Wiki Manager and so would
//     silently leave the Role picker empty for the exact persona this screen
//     exists for.
//   - Person options: `jarvis.chat.custom_skills_api.list_shareable_users`
//     (the ShareDialog.vue endpoint — "listShareableUsers-style endpoint"
//     the build brief pointed at) — enabled users, excluding the caller.
import { ref, reactive, computed, watch } from "vue";
import {
	Autocomplete,
	Badge,
	Button,
	Dialog,
	FeatherIcon,
	FormControl,
	LoadingIndicator,
	Switch,
	toast,
	confirmDialog,
} from "frappe-ui";
import { listShareableUsers } from "@/api";
import {
	listRoleOptions,
	listQuestionRules,
	saveQuestionRule,
	deleteQuestionRule,
	getPersonalisationSettings,
	setPersonalisationSettings,
	generateChatQuestionsNow,
} from "@/api/personalise";
import { timeAgo } from "@/utils/datetime";
import { agentName } from "@/branding";

const props = defineProps({
	open: { type: Boolean, default: false },
});
const emit = defineEmits(["update:open"]);

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}
function intOr(v, d) {
	if (v === null || v === undefined || v === "") return d;
	const n = Number(v);
	return Number.isFinite(n) ? n : d;
}

function close() {
	emit("update:open", false);
}
function onClosed() {
	// drop the inline editor so the next open never flashes a stale draft
	editor.show = false;
}

// ── left nav ─────────────────────────────────────────────────────────────────
const SECTIONS = [
	{ label: "Questions", value: "questions" },
	{ label: "Limits", value: "limits" },
];
const section = ref("questions");

// ── question rules ──────────────────────────────────────────────────────────
const SCOPE_OPTIONS = [
	{ label: "Everyone in the org", value: "Org" },
	{ label: "People with a role", value: "Role" },
	{ label: "One person", value: "User" },
];
// Same theme convention as WikiTab's scope badge (research/design-language.md
// §6: gray=neutral, blue=info, green=success/personal).
const SCOPE_THEME = { Org: "gray", Role: "blue", User: "green" };

const rules = ref([]);
const rulesLoading = ref(false);
const roleOptions = ref([]); // [{label, value}] — Role Autocomplete
const userOptions = ref([]); // [{label, value}] — Person Autocomplete

function userLabel(id) {
	const u = userOptions.value.find((o) => o.value === id);
	return (u && u.label) || id;
}
function scopeLabel(rule) {
	if (rule.scope === "Role") return `Role: ${rule.target_role || "—"}`;
	if (rule.scope === "User") return `Person: ${userLabel(rule.target_user)}`;
	return "Everyone";
}

async function loadRules() {
	rulesLoading.value = true;
	try {
		rules.value = (await listQuestionRules()) || [];
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		rulesLoading.value = false;
	}
}

async function loadTargetOptions() {
	try {
		const roles = await listRoleOptions();
		roleOptions.value = (roles || []).map((r) => ({ label: r, value: r }));
	} catch (e) {
		roleOptions.value = [];
	}
	try {
		const users = await listShareableUsers();
		userOptions.value = (users || []).map((u) => ({
			label: u.full_name || u.name,
			value: u.name,
		}));
	} catch (e) {
		userOptions.value = [];
	}
}

// immediate-save Switch (row toggle), rollback on failure
const rowActing = ref("");
async function toggleActive(rule, value) {
	const prev = rule.active;
	rule.active = value ? 1 : 0;
	rowActing.value = rule.name;
	try {
		await saveQuestionRule({ name: rule.name, active: value ? 1 : 0 });
		toast.success(value ? "Question enabled" : "Question paused");
	} catch (e) {
		rule.active = prev;
		toast.error(errMsg(e));
	} finally {
		rowActing.value = "";
	}
}

// inline editor (create + edit share one card, per DESIGN.md §5)
const editor = reactive({
	show: false,
	name: "",
	question: "",
	context_md: "",
	scope: "Org",
	target_role: "",
	target_user: "",
	active: true,
	saving: false,
});

function openNew() {
	editor.show = true;
	editor.name = "";
	editor.question = "";
	editor.context_md = "";
	editor.scope = "Org";
	editor.target_role = "";
	editor.target_user = "";
	editor.active = true;
}
function openEdit(rule) {
	editor.show = true;
	editor.name = rule.name;
	editor.question = rule.question || "";
	editor.context_md = rule.context_md || "";
	editor.scope = rule.scope || "Org";
	editor.target_role = rule.target_role || "";
	editor.target_user = rule.target_user || "";
	editor.active = !!rule.active;
}
function closeEditor() {
	editor.show = false;
}

const canSaveEditor = computed(() => {
	if (!editor.question.trim()) return false;
	if (editor.scope === "Role" && !editor.target_role) return false;
	if (editor.scope === "User" && !editor.target_user) return false;
	return true;
});

async function saveEditor() {
	if (!canSaveEditor.value) return;
	editor.saving = true;
	try {
		const payload = {
			question: editor.question.trim(),
			context_md: editor.context_md,
			scope: editor.scope,
			target_role: editor.scope === "Role" ? editor.target_role : "",
			target_user: editor.scope === "User" ? editor.target_user : "",
			active: editor.active ? 1 : 0,
		};
		if (editor.name) payload.name = editor.name;
		await saveQuestionRule(payload);
		toast.success(editor.name ? "Question updated" : "Question added");
		closeEditor();
		await loadRules();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		editor.saving = false;
	}
}

function confirmDeleteRule(rule) {
	confirmDialog({
		title: "Delete this question?",
		message: `Removes this configured question. Questions ${agentName} already asked people from this rule stay in their own banks — this only stops new ones.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await deleteQuestionRule(rule.name);
				hideDialog();
				toast.success("Question removed");
				loadRules();
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}

// ── limits (personalise settings) ───────────────────────────────────────────
const settings = reactive({
	daily_question_cap: 5,
	personalise_enabled: true,
	chat_question_mining_enabled: true,
	chat_mining_last_run_at: null,
	chat_mining_last_run_status: "",
});
const settingsLoading = ref(false);
const savingSettings = ref(false);
const generatingNow = ref(false);

const chatMiningLastRunAgo = computed(() =>
	settings.chat_mining_last_run_at ? ` ${timeAgo(settings.chat_mining_last_run_at)}` : ""
);

async function loadSettings() {
	settingsLoading.value = true;
	try {
		const s = await getPersonalisationSettings();
		settings.daily_question_cap = intOr(s.daily_question_cap, 5);
		settings.personalise_enabled = !!s.personalise_enabled;
		settings.chat_question_mining_enabled = !!s.chat_question_mining_enabled;
		settings.chat_mining_last_run_at = s.chat_mining_last_run_at || null;
		settings.chat_mining_last_run_status = s.chat_mining_last_run_status || "";
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		settingsLoading.value = false;
	}
}

async function saveSettings() {
	savingSettings.value = true;
	try {
		const res = await setPersonalisationSettings({
			daily_question_cap: Number(settings.daily_question_cap) || 0,
			personalise_enabled: settings.personalise_enabled ? 1 : 0,
			chat_question_mining_enabled: settings.chat_question_mining_enabled ? 1 : 0,
		});
		settings.daily_question_cap = intOr(res.daily_question_cap, settings.daily_question_cap);
		settings.personalise_enabled = !!res.personalise_enabled;
		settings.chat_question_mining_enabled = !!res.chat_question_mining_enabled;
		settings.chat_mining_last_run_at = res.chat_mining_last_run_at || null;
		settings.chat_mining_last_run_status = res.chat_mining_last_run_status || "";
		toast.success("Settings saved");
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		savingSettings.value = false;
	}
}

async function generateNow() {
	generatingNow.value = true;
	try {
		const res = await generateChatQuestionsNow();
		if (res && res.ok) {
			toast.success("Mining recent chats — new questions will appear shortly.");
			// The job runs in the background (queue 'long'); poll the last-run
			// status a few times so the line under the button reflects the result
			// ("… N questions" or "no new chat activity") without reopening the dialog.
			pollMiningStatus();
		} else {
			toast.info((res && res.reason) || "Already running.");
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		generatingNow.value = false;
	}
}

function pollMiningStatus() {
	let tries = 0;
	const before = settings.chat_mining_last_run_at;
	const tick = async () => {
		tries += 1;
		try {
			const s = await getPersonalisationSettings();
			settings.chat_mining_last_run_at = s.chat_mining_last_run_at || null;
			settings.chat_mining_last_run_status = s.chat_mining_last_run_status || "";
			// Stop once the run stamped a newer timestamp, or after ~30s.
			if (settings.chat_mining_last_run_at !== before || tries >= 6) return;
		} catch (e) {
			/* best-effort */
		}
		setTimeout(tick, 5000);
	};
	setTimeout(tick, 4000);
}

// ── init: (re)load everything fresh every time the dialog opens ─────────────
watch(
	() => props.open,
	(v) => {
		if (!v) return;
		loadRules();
		loadSettings();
		loadTargetOptions();
	}
);
</script>
