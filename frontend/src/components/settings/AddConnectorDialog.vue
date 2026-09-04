<template>
	<Dialog v-model="show" :options="{ title: dialogTitle, size: 'lg' }" @after-leave="onClosed">
		<template #body-content>
			<!-- ── Step 1: connect ─────────────────────────────────────────── -->
			<div v-if="step === 1" class="flex flex-col gap-3">
				<p class="text-sm text-ink-gray-6">
					{{ agentName }} can use this connector's actions in chat, gated by what you
					allow on the next step.
				</p>

				<div class="flex items-end gap-2">
					<FormControl
						class="flex-1"
						type="select"
						label="App"
						:options="presetOptions"
						:disabled="isEdit"
						:modelValue="form.preset"
						@update:modelValue="onPresetChange"
					/>
					<ConnectorLogo :preset="form.preset" :size="20" class="mb-2 text-ink-gray-5" />
				</div>

				<FormControl
					v-if="form.preset === 'Custom URL' && allowCustomUrls"
					type="text"
					label="Base URL"
					placeholder="https://example.com/mcp"
					:modelValue="form.base_url"
					@update:modelValue="(v) => onBaseUrlChange(v)"
				/>

				<FormControl
					type="password"
					label="Access token"
					:placeholder="
						isEdit ? 'Leave blank to keep the saved token' : 'Paste your token'
					"
					:modelValue="form.credential"
					@update:modelValue="(v) => onCredentialChange(v)"
				/>

				<div class="flex items-center gap-3 rounded-lg border p-3">
					<Button
						variant="subtle"
						:label="testing ? 'Testing…' : 'Test connection'"
						:loading="testing"
						:disabled="!canTest"
						@click="runTest"
					/>
					<span v-if="testState.status === 'passed'" class="text-sm text-ink-green-3">
						Connected, {{ testState.tools.length }}
						{{ testState.tools.length === 1 ? "tool" : "tools" }} found
					</span>
					<span v-else-if="testState.status === 'failed'" class="text-sm text-ink-red-4">
						{{ testState.message }}
					</span>
					<span v-else class="text-sm text-ink-gray-5">Not tested yet</span>
				</div>
			</div>

			<!-- ── Step 2: allowed actions ─────────────────────────────────── -->
			<div v-else class="flex flex-col gap-3">
				<div class="flex items-center justify-between gap-3">
					<p class="text-sm text-ink-gray-6">
						Choose what {{ agentName }} may do with {{ connectorDisplayName }}.
						Read-only actions are pre-checked; writes are off by default.
					</p>
					<Button
						variant="ghost"
						size="sm"
						label="Allow all read-only"
						@click="allowAllReadOnly"
					/>
				</div>

				<FormControl
					type="text"
					placeholder="Search actions"
					:modelValue="actionQuery"
					@update:modelValue="(v) => (actionQuery = v)"
				/>

				<div class="flex max-h-96 flex-col gap-4 overflow-y-auto">
					<div v-if="filteredReadOnly.length">
						<div class="mb-1 text-xs-medium uppercase tracking-wide text-ink-gray-5">
							Read-only
						</div>
						<div class="flex flex-col gap-1">
							<label
								v-for="a in filteredReadOnly"
								:key="a.action"
								class="flex cursor-pointer items-start justify-between gap-3 rounded px-2 py-1.5 hover:bg-surface-gray-2"
							>
								<span class="min-w-0">
									<span class="block truncate text-sm text-ink-gray-8">{{
										a.action
									}}</span>
									<span
										v-if="a.description"
										class="block truncate text-xs text-ink-gray-5"
										>{{ a.description }}</span
									>
								</span>
								<Switch
									:modelValue="!!selected[a.action]"
									@update:modelValue="(v) => setActionAllowed(a.action, v)"
								/>
							</label>
						</div>
					</div>

					<div v-if="filteredWrites.length">
						<div class="mb-1 text-xs-medium uppercase tracking-wide text-ink-gray-5">
							Writes
						</div>
						<div class="flex flex-col gap-1">
							<label
								v-for="a in filteredWrites"
								:key="a.action"
								class="flex cursor-pointer items-start justify-between gap-3 rounded px-2 py-1.5 hover:bg-surface-gray-2"
							>
								<span class="min-w-0">
									<span class="flex items-center gap-1.5">
										<span class="truncate text-sm text-ink-gray-8">{{
											a.action
										}}</span>
										<Badge
											v-if="a.destructive"
											theme="red"
											variant="subtle"
											size="sm"
											label="Destructive"
										/>
									</span>
									<span
										v-if="a.description"
										class="block truncate text-xs text-ink-gray-5"
										>{{ a.description }}</span
									>
								</span>
								<Switch
									:modelValue="!!selected[a.action]"
									@update:modelValue="(v) => setActionAllowed(a.action, v)"
								/>
							</label>
						</div>
					</div>

					<p
						v-if="!filteredReadOnly.length && !filteredWrites.length"
						class="py-6 text-center text-p-sm text-ink-gray-5"
					>
						No actions match your search.
					</p>
				</div>
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button v-if="step === 2" label="Back" :disabled="saving" @click="step = 1" />
				<Button label="Cancel" :disabled="saving" @click="cancel" />
				<Button
					v-if="step === 1"
					variant="solid"
					label="Continue"
					:disabled="testState.status !== 'passed'"
					@click="step = 2"
				/>
				<Button
					v-if="step === 2"
					variant="solid"
					label="Save"
					:loading="saving"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// Add/edit an MCP connector (MCP_CONNECTORS_PLAN.md P4). Two steps, copying
// PromotionRequestDialog's Dialog structure (#body-content/#actions slots,
// FormControl type="select" for the preset picker rather than Autocomplete —
// same documented trap: frappe-ui's Autocomplete search popover renders
// outside a reka-ui Dialog's focus scope and is unclickable there).
//
// test_connector needs a SAVED row name, so step 1's "Test connection" both
// persists the connect fields (add_connector the first time this session,
// update_connector after) and runs the live probe in one click. Step 2 (the
// allowed-actions picker) is only reachable once that test has passed, which
// is what guarantees a row already exists by the time Save calls
// set_allowed_actions. Cancelling out of a freshly-created, still-unsaved row
// deletes it so a browser-closed-mid-dialog never leaves an orphan.
//
// Edit mode (`connector` prop set) reuses the same shape: the preset is fixed
// (pinned server-side, see connectors_api.add_connector), "Test connection"
// calls update_connector instead of add_connector, and a blank credential
// means "keep the saved one" (connectors_api.update_connector's contract).
import { computed, reactive, ref, watch } from "vue";
import { Badge, Button, Dialog, FormControl, Switch, toast } from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";
import {
	addConnector,
	deleteConnector,
	setConnectorAllowedActions,
	testConnector,
	updateConnector,
} from "@/api";
import { agentName } from "@/branding";
import { errHtml, errMessage } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// "Shared" or "Personal" — which section's "Add connector" button opened
	// this dialog. Ignored in edit mode (the row's own scope never changes here).
	scope: { type: String, default: "Personal" },
	allowCustomUrls: { type: Boolean, default: true },
	// The row being edited, or null for a fresh Add.
	connector: { type: Object, default: null },
});
const emit = defineEmits(["update:modelValue", "saved"]);

const PRESETS = ["GitHub", "Atlassian", "Linear", "Stripe", "Custom URL"];

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const isEdit = computed(() => !!props.connector);
const dialogTitle = computed(() => (isEdit.value ? "Edit connector" : "Add connector"));
const presetOptions = computed(() =>
	PRESETS.filter((p) => p !== "Custom URL" || props.allowCustomUrls).map((p) => ({
		label: p,
		value: p,
	}))
);
const step = ref(1);
const saving = ref(false);
const testing = ref(false);

const form = reactive({ preset: "GitHub", base_url: "", credential: "" });
// Step 2's "Choose what {agent} may do with X" line. There's no user-typed
// Name field any more (the backend derives the saved label - preset display
// name, or the Custom URL's hostname), so this falls back to the preset's
// own name for a fresh Add and to the edited row's already-saved label
// otherwise.
const connectorDisplayName = computed(() => {
	if (isEdit.value && props.connector?.label) return props.connector.label;
	if (form.preset && form.preset !== "Custom URL") return form.preset;
	return "this connector";
});
// The saved row this dialog is working against: the edited row's name, or the
// name add_connector returned the first time "Test connection" ran this session.
const rowName = ref("");
// Only true for a row THIS dialog session created (never for an edited row) —
// gates the delete-on-close cleanup below.
const createdThisSession = ref(false);
// Flips true once Save has actually committed, so a created-this-session row
// that WAS saved is never mistaken for an orphan.
const savedThisSession = ref(false);
const testState = reactive({ status: "idle", tools: [], message: "" }); // idle | passed | failed

function resetForCreate() {
	form.preset = "GitHub";
	form.base_url = "";
	form.credential = "";
	rowName.value = "";
	createdThisSession.value = false;
	savedThisSession.value = false;
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	step.value = 1;
	selected.value = {};
	touchedActions.value = new Set();
	actionQuery.value = "";
}
function resetForEdit(row) {
	form.preset = row.preset || "GitHub";
	form.base_url = row.base_url || "";
	form.credential = "";
	rowName.value = row.name;
	createdThisSession.value = false;
	savedThisSession.value = false;
	// An edited row may already have a passing test on record; that state is
	// server truth (last_test_status), not something to re-derive here — but
	// this dialog only knows the LIVE tools/list shape after a fresh test, so
	// it still starts at "idle" and asks for a re-test before Continue unlocks.
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	step.value = 1;
	selected.value = {};
	touchedActions.value = new Set();
	actionQuery.value = "";
}

watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		if (props.connector) resetForEdit(props.connector);
		else resetForCreate();
	}
);

// Any change to what's actually tested (preset/base_url/credential) invalidates
// a prior pass, same as the backend's own last_test_status reset on a real
// base_url/credential change.
watch([() => form.preset, () => form.base_url], () => {
	if (testState.status !== "idle") {
		testState.status = "idle";
		testState.tools = [];
		testState.message = "";
	}
});
function onPresetChange(v) {
	form.preset = v;
	form.base_url = "";
}
function onBaseUrlChange(v) {
	form.base_url = v;
}
function onCredentialChange(v) {
	form.credential = v;
	if (testState.status !== "idle") {
		testState.status = "idle";
		testState.tools = [];
		testState.message = "";
	}
}

const canTest = computed(() => {
	if (form.preset === "Custom URL" && !form.base_url.trim()) return false;
	if (!isEdit.value && !form.credential.trim()) return false;
	return true;
});

// Custom URL is the one preset add_connector cannot derive a `key` for
// (jarvis_connector.py._normalize_key requires a non-empty slug); every named
// preset gets its key from the backend's own _PRESET_KEYS. Derived from the
// host so two different custom endpoints don't collide on the same key.
function slugifyKey(text) {
	const slug = (text || "")
		.toLowerCase()
		.replace(/[^a-z0-9_-]+/g, "-")
		.replace(/^-+/, "")
		.slice(0, 64);
	return slug || "custom";
}
function customUrlKey(url) {
	try {
		return slugifyKey(new URL(url).hostname);
	} catch (e) {
		return slugifyKey(url);
	}
}

async function runTest() {
	if (!canTest.value || testing.value) return;
	testing.value = true;
	try {
		if (!rowName.value) {
			// First test this session, create mode: mint the row. No `label` is
			// sent - add_connector derives one server-side (the preset's display
			// name, or the Custom URL's hostname).
			const row = await addConnector({
				preset: form.preset,
				base_url: form.base_url.trim(),
				scope: props.scope,
				credential: form.credential,
				...(form.preset === "Custom URL"
					? { key: customUrlKey(form.base_url.trim()) }
					: {}),
			});
			rowName.value = row.name;
			createdThisSession.value = true;
		} else {
			// Re-test (edit mode, or a second Test press this session): persist
			// whatever changed first. Blank credential means "keep the saved one".
			await updateConnector(rowName.value, {
				...(form.preset === "Custom URL" ? { base_url: form.base_url.trim() } : {}),
				...(form.credential.trim() ? { credential: form.credential.trim() } : {}),
			});
		}
		const res = await testConnector(rowName.value);
		if (res && res.ok) {
			testState.status = "passed";
			testState.tools = res.tools || [];
			testState.message = "";
		} else {
			testState.status = "failed";
			testState.tools = [];
			testState.message =
				(res && res.error && res.error.message) || "Could not reach the connector.";
		}
	} catch (e) {
		testState.status = "failed";
		testState.tools = [];
		// Rendered via {{ }} (a text sink), not v-html - plain text, not escaped HTML.
		testState.message = errMessage(e);
	} finally {
		testing.value = false;
	}
}

// ── step 2: allowed actions ─────────────────────────────────────────────────
const selected = ref({}); // action -> bool (display / working value)
// Actions the user actually touched this session (a Switch flip, or "Allow
// all read-only"). test_connector's response carries each action's stored
// `allowed` grant (its server-merged value: read-only pre-checked, writes off
// the first time; an admin's PRIOR choice preserved on an edit re-test), so the
// picker shows the connector's true current grants. Save still sends ONLY the
// touched subset; set_allowed_actions keeps every unmentioned action's existing
// stored value (its own documented contract).
const touchedActions = ref(new Set());
const actionQuery = ref("");

watch(
	() => testState.tools,
	(tools) => {
		const next = {};
		// Fall back to read_only if `allowed` is absent (older backend response).
		for (const t of tools)
			next[t.action] = t.allowed !== undefined ? !!t.allowed : !!t.read_only;
		selected.value = next;
		touchedActions.value = new Set();
	}
);

function setActionAllowed(action, value) {
	selected.value[action] = value;
	touchedActions.value.add(action);
}

const readOnlyTools = computed(() => testState.tools.filter((t) => t.read_only));
const writeTools = computed(() => testState.tools.filter((t) => !t.read_only));
function matchesQuery(t) {
	const q = actionQuery.value.trim().toLowerCase();
	if (!q) return true;
	return t.action.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q);
}
const filteredReadOnly = computed(() => readOnlyTools.value.filter(matchesQuery));
const filteredWrites = computed(() => writeTools.value.filter(matchesQuery));

function allowAllReadOnly() {
	// A deliberate bulk action - every read-only action counts as touched, even
	// ones the search filter is currently hiding.
	for (const t of readOnlyTools.value) setActionAllowed(t.action, true);
}

async function save() {
	if (!rowName.value) return;
	saving.value = true;
	try {
		const actions = testState.tools
			.filter((t) => touchedActions.value.has(t.action))
			.map((t) => ({ action: t.action, allowed: !!selected.value[t.action] }));
		await setConnectorAllowedActions(rowName.value, actions);
		const row = await updateConnector(rowName.value, { enabled: 1 });
		savedThisSession.value = true;
		toast.success(isEdit.value ? "Connector updated" : "Connector added");
		emit("saved", row);
		show.value = false;
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

function cancel() {
	if (saving.value) return;
	show.value = false;
}

// Fires on EVERY close - Cancel, the dialog's own X, Escape, and a backdrop
// click all set modelValue false via v-model and land here, not just the
// Cancel button, which is why the orphan-delete lives here rather than in
// cancel() above. Only a row THIS session created and never saved is an
// orphan worth cleaning up - an edited row pre-dates this dialog and stays
// exactly as update_connector last left it.
async function onClosed() {
	if (createdThisSession.value && !savedThisSession.value && rowName.value) {
		try {
			await deleteConnector(rowName.value);
		} catch (e) {
			/* best-effort cleanup */
		}
	}
	// Drop any in-memory tool list so the next open never flashes stale actions.
	testState.status = "idle";
	testState.tools = [];
	selected.value = {};
	touchedActions.value = new Set();
}
</script>
