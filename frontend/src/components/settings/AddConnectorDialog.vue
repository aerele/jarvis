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

				<template v-if="form.preset === 'Custom URL' && allowCustomUrls">
					<div class="flex items-end gap-2">
						<FormControl
							class="flex-1"
							type="text"
							label="Base URL"
							placeholder="https://example.com/mcp"
							:modelValue="form.base_url"
							@update:modelValue="(v) => onBaseUrlChange(v)"
						/>
						<Button
							variant="subtle"
							:label="probing ? 'Checking…' : 'Check'"
							:loading="probing"
							:disabled="!form.base_url.trim()"
							@click="runProbe"
						/>
					</div>
					<p v-if="probeError" class="text-xs text-ink-red-4">{{ probeError }}</p>
				</template>

				<!-- OAuth-first (design §1): a preset with a sign-in option defaults
				     here. The pasted-token path stays one click away via "Use a token
				     instead" - shown only before a row exists this session (rowName),
				     so switching mid-flow never has to migrate an already-saved row's
				     auth method. "Sign-in option" means whatever the catalog marks
				     connected_app/dcr/static (jarvis/connectors/catalog.py) - token and
				     open presets never reach this branch. Custom URL only earns it once
				     Check finds a sign-in requirement (customUrlOauth.active) - a plain
				     token-only server never leaves the FormControl-only path below. -->
				<template v-if="presetHasOauth && form.auth_method === 'OAuth'">
					<div
						v-if="!rowOauthConnected"
						class="flex flex-col gap-2 rounded-lg border p-3"
					>
						<p
							v-if="presetAuthClass === 'connected_app'"
							class="text-xs text-ink-gray-5"
						>
							Sign in with your {{ form.preset }} account to connect.
						</p>
						<p
							v-else-if="form.preset !== 'Custom URL'"
							class="text-xs text-ink-gray-5"
						>
							Sign in to {{ form.preset }} to connect.
						</p>
						<p v-else-if="customUrlOauth.signinHost" class="text-xs text-ink-gray-5">
							This app signs you in at {{ customUrlOauth.signinHost }}.
						</p>

						<!-- A static (admin-registered) server needs a client id/secret
						     before anyone can sign in. The row already exists (the first
						     Connect press creates it) but connecting was deliberately
						     skipped until this is filled in - see startOauthConnect. Gated
						     on rowNeedsStaticClient alone (server truth), not the preset, so
						     a named static preset gets the same block Custom URL already
						     had. -->
						<template v-if="rowNeedsStaticClient">
							<template v-if="isAdmin">
								<FormControl
									type="text"
									label="Client ID"
									:modelValue="staticClient.id"
									@update:modelValue="(v) => (staticClient.id = v)"
								/>
								<FormControl
									type="password"
									label="Client secret"
									:modelValue="staticClient.secret"
									@update:modelValue="(v) => (staticClient.secret = v)"
								/>
								<div v-if="rowRedirectUri" class="flex flex-col gap-1">
									<span class="text-xs text-ink-gray-5"
										>Callback URL to register with the app</span
									>
									<div class="flex items-center gap-2">
										<code
											class="min-w-0 flex-1 truncate rounded border px-2 py-1 text-xs text-ink-gray-7"
											>{{ rowRedirectUri }}</code
										>
										<Button
											variant="ghost"
											icon="copy"
											:tooltip="copied ? 'Copied' : 'Copy'"
											@click="copyRedirectUri"
										/>
									</div>
								</div>
								<Button
									variant="solid"
									label="Save"
									:loading="savingClient"
									:disabled="
										!staticClient.id.trim() || !staticClient.secret.trim()
									"
									class="self-start"
									@click="saveStaticClient"
								/>
							</template>
							<p v-else class="text-xs text-ink-gray-5">
								Ask your admin to finish setup.
							</p>
						</template>

						<Button
							v-else
							variant="solid"
							:label="oauthConnectLabel"
							:loading="connecting"
							class="self-start"
							@click="startOauthConnect"
						/>
					</div>
					<Button
						v-if="!rowName"
						variant="ghost"
						size="sm"
						label="Use a token instead"
						class="self-start"
						@click="switchAuthMethod('API Key')"
					/>
				</template>

				<template v-else-if="presetAuthClass === 'open'">
					<p class="text-xs text-ink-gray-5">No sign-in needed.</p>
				</template>

				<template v-else>
					<FormControl
						type="password"
						label="Access token"
						:placeholder="
							isEdit ? 'Leave blank to keep the saved token' : 'Paste your token'
						"
						:modelValue="form.credential"
						@update:modelValue="(v) => onCredentialChange(v)"
					/>
					<p v-if="tokenHint || tokenDocsUrl" class="text-xs text-ink-gray-5">
						{{ tokenHint }}
						<a
							v-if="tokenDocsUrl"
							:href="tokenDocsUrl"
							target="_blank"
							rel="noopener"
							class="text-ink-blue-link hover:underline"
						>
							How to create this token
						</a>
					</p>
					<Button
						v-if="presetHasOauth && !rowName"
						variant="ghost"
						size="sm"
						:label="`Sign in to ${form.preset} instead`"
						class="self-start"
						@click="switchAuthMethod('OAuth')"
					/>
				</template>

				<div
					v-if="form.auth_method !== 'OAuth' || rowOauthConnected"
					class="flex items-center gap-3 rounded-lg border p-3"
				>
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
import { CUSTOM_URL_TOKEN_HINT } from "@/components/settings/connectorHelp.js";
import {
	addConnector,
	connectOauth,
	deleteConnector,
	probeConnectorAuth,
	setConnectorAllowedActions,
	setOauthClientCredentials,
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
	// listConnectors()'s catalog: [{ name, key, auth, category, logo, help_url,
	// hint }], enabled providers in catalog order. Drives the preset picker and
	// every auth-class branch below instead of a hardcoded list.
	catalog: { type: Array, default: () => [] },
});
const emit = defineEmits(["update:modelValue", "saved"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const isAdmin = !!window.is_system_manager || !!window.is_jarvis_admin;

const isEdit = computed(() => !!props.connector);
const dialogTitle = computed(() => (isEdit.value ? "Edit connector" : "Add connector"));
const presetOptions = computed(() => {
	const opts = props.catalog.map((c) => ({ label: c.name, value: c.name }));
	if (props.allowCustomUrls) opts.push({ label: "Custom URL", value: "Custom URL" });
	return opts;
});
const step = ref(1);
const saving = ref(false);
const testing = ref(false);

const form = reactive({ preset: "", base_url: "", credential: "", auth_method: "API Key" });
// resetForCreate/resetForEdit always set preset+auth_method before the dialog
// is shown, so these are just safe empty defaults, not a real first preset.

// name -> catalog auth class ("dcr"/"static"/"token"/"open"/"connected_app"),
// or null for Custom URL (not a catalog entry) or an unknown/not-yet-loaded name.
function catalogAuthOf(name) {
	if (name === "Custom URL") return null;
	const entry = props.catalog.find((c) => c.name === name);
	return entry ? entry.auth : null;
}
// The picked preset's catalog auth class, or "custom" for Custom URL (its
// class is decided live by the Check probe instead, see customUrlOauth).
const presetAuthClass = computed(() =>
	form.preset === "Custom URL" ? "custom" : catalogAuthOf(form.preset)
);
// connected_app/dcr/static all default to a sign-in flow (OAUTH_CONNECTORS_DESIGN.md
// §3, extended to every catalog auth class that supports it); token/open never do.
function presetDefaultsToOauth(auth) {
	return auth === "connected_app" || auth === "dcr" || auth === "static";
}
// Whether the selected preset offers a sign-in option at all - gates every
// OAuth-mode template branch below. Custom URL only joins this once Check
// (runProbe) finds the pasted server needs a sign-in - it never defaults to
// it the way a named preset does.
const presetHasOauth = computed(() => {
	if (form.preset === "Custom URL") return customUrlOauth.active;
	return presetDefaultsToOauth(presetAuthClass.value);
});
// The sign-in box's own button label: a Connected App keeps its named "Sign
// in to X" (today's GitHub copy); every discovered flow (dcr/static/Custom
// URL) gets the generic "Connect" (design §8 copy - nothing to brand it with
// beyond the preset name already shown in the line above the button).
const oauthConnectLabel = computed(() => {
	if (connecting.value) return "Connecting…";
	return presetAuthClass.value === "connected_app" ? `Sign in to ${form.preset}` : "Connect";
});
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
// Per-preset token guidance shown under the Access token field now comes
// straight from the catalog entry (jarvis/connectors/catalog.py): its own
// hint text plus a link to help_url when the vendor has one. Custom URL has
// no catalog entry, so it gets the one generic hint below instead - a named
// preset with neither a hint nor a help_url renders no guidance line at all
// (see the template's v-if on this).
const tokenHint = computed(() => {
	if (form.preset === "Custom URL") return CUSTOM_URL_TOKEN_HINT;
	const entry = props.catalog.find((c) => c.name === form.preset);
	return entry?.hint || "";
});
const tokenDocsUrl = computed(() => {
	if (form.preset === "Custom URL") return "";
	const entry = props.catalog.find((c) => c.name === form.preset);
	return entry?.help_url || "";
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
// Whether THIS row currently has a live per-user connection (design §6a) -
// true for an edited row that's already connected, or right after this
// session's own sign-in round-trip reopens the dialog. Gates whether step 1
// shows the "Sign in" prompt or the Test connection box.
const rowOauthConnected = ref(false);
const connecting = ref(false);
// Custom URL sign-in probe (design §8: paste a URL -> Check -> maybe sign-in).
// `active` is what lets Custom URL join presetHasOauth above; a plain
// token-only server never sets it.
const customUrlOauth = reactive({ active: false, signinHost: "", registration: "" });
const probing = ref(false);
const probeError = ref("");
// Static-registration rows (design §9's "static" mode) need an admin to enter
// a client id/secret before anyone can sign in - set from whatever
// add_connector / set_oauth_client_credentials last returned, alongside the
// callback URL the admin must register at the provider.
const rowNeedsStaticClient = ref(false);
const rowRedirectUri = ref("");
const staticClient = reactive({ id: "", secret: "" });
const savingClient = ref(false);
// "Copied" flash on the callback-URL copy button, same idiom as
// DirectSubscriptionCard's own copy button.
const copied = ref(false);

// Shared by resetForCreate/resetForEdit/onPresetChange so the Check state
// never survives a swap to a different preset or a fresh Add.
function resetCustomUrlOauthState() {
	customUrlOauth.active = false;
	customUrlOauth.signinHost = "";
	customUrlOauth.registration = "";
	probing.value = false;
	probeError.value = "";
	rowNeedsStaticClient.value = false;
	rowRedirectUri.value = "";
	staticClient.id = "";
	staticClient.secret = "";
	savingClient.value = false;
	copied.value = false;
}

// The picker's opening preset: the catalog's own first entry (its order is
// the shipped-first/category order jarvis/connectors/catalog.py documents),
// or Custom URL when a tenant has no catalog presets enabled for it.
function defaultPreset() {
	if (props.catalog.length) return props.catalog[0].name;
	return props.allowCustomUrls ? "Custom URL" : "";
}

function resetForCreate() {
	form.preset = defaultPreset();
	form.base_url = "";
	form.credential = "";
	form.auth_method = presetDefaultsToOauth(catalogAuthOf(form.preset)) ? "OAuth" : "API Key";
	rowName.value = "";
	createdThisSession.value = false;
	savedThisSession.value = false;
	rowOauthConnected.value = false;
	connecting.value = false;
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	step.value = 1;
	selected.value = {};
	touchedActions.value = new Set();
	actionQuery.value = "";
	resetCustomUrlOauthState();
}
function resetForEdit(row) {
	form.preset = row.preset || defaultPreset();
	form.base_url = row.base_url || "";
	form.credential = "";
	form.auth_method = row.auth_method || "API Key";
	rowName.value = row.name;
	createdThisSession.value = false;
	savedThisSession.value = false;
	rowOauthConnected.value = !!row.oauth_connected;
	connecting.value = false;
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
	resetCustomUrlOauthState();
	// A Custom URL row already on the sign-in path (opened fresh, or reopened
	// after the provider redirects back) - re-derive the same state a live
	// Check/Connect would have set, so reopening never needs a re-Check.
	if (row.preset === "Custom URL" && row.auth_method === "OAuth") {
		customUrlOauth.active = true;
		customUrlOauth.signinHost = row.signin_host || "";
	}
	rowNeedsStaticClient.value = !!row.needs_static_client;
	rowRedirectUri.value = row.oauth_redirect_uri || "";
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
// Editing the URL invalidates a prior Check the same way it invalidates a
// prior Test above - the previous probe result no longer describes what's
// typed, so a re-Check is required before Connect can show up again. Guarded
// on !rowName for the same reason switchAuthMethod's own toggle links are
// (see the template): once a row exists this session, nothing here may
// migrate its already-saved auth method out from under it.
watch(
	() => form.base_url,
	() => {
		if (rowName.value) return;
		if (!customUrlOauth.active && !probeError.value) return;
		resetCustomUrlOauthState();
		if (form.preset === "Custom URL") form.auth_method = "API Key";
	}
);
function onPresetChange(v) {
	form.preset = v;
	form.base_url = "";
	// OAuth-first (design §1): switching to a preset with a sign-in option
	// re-defaults to it, same as the dialog's own initial state.
	form.auth_method = presetDefaultsToOauth(catalogAuthOf(v)) ? "OAuth" : "API Key";
	rowOauthConnected.value = false;
	resetCustomUrlOauthState();
}

// The toggle link only shows before this session has created a row (see the
// template), so this never has to migrate an already-saved row's auth
// method - it just flips which half of step 1 renders next.
function switchAuthMethod(method) {
	form.auth_method = method;
	form.credential = "";
	rowOauthConnected.value = false;
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
}

// Applies an add_connector / connect_oauth / set_oauth_client_credentials row
// summary's OAuth-setup fields onto local state, so every call site that gets
// a fresh row back (creating it, saving static creds) stays in sync the same
// way.
function applyOauthRowMeta(row) {
	if (!row) return;
	rowOauthConnected.value = !!row.oauth_connected;
	rowNeedsStaticClient.value = !!row.needs_static_client;
	rowRedirectUri.value = row.oauth_redirect_uri || "";
	if (row.signin_host) customUrlOauth.signinHost = row.signin_host;
}

async function startOauthConnect() {
	if (connecting.value) return;
	connecting.value = true;
	try {
		if (!rowName.value) {
			const row = await addConnector({
				preset: form.preset,
				scope: props.scope,
				auth_method: "OAuth",
				...(form.preset === "Custom URL"
					? { base_url: form.base_url.trim(), key: customUrlKey(form.base_url.trim()) }
					: {}),
			});
			rowName.value = row.name;
			createdThisSession.value = true;
			applyOauthRowMeta(row);
			// A static-client row needs an admin to enter credentials before
			// anyone can sign in - stop here rather than redirect into a
			// sign-in that cannot succeed yet (the credentials block renders
			// instead; see the template).
			if (rowNeedsStaticClient.value) return;
		}
		const res = await connectOauth(rowName.value);
		if (res && res.ok && res.url) {
			window.location.href = res.url;
			return;
		}
		toast.error(
			errHtml(
				{ message: (res && res.error && res.error.message) || "" },
				"Could not sign in."
			)
		);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		connecting.value = false;
	}
}

async function saveStaticClient() {
	if (!rowName.value || savingClient.value) return;
	const id = staticClient.id.trim();
	const secret = staticClient.secret.trim();
	if (!id || !secret) return;
	savingClient.value = true;
	try {
		const row = await setOauthClientCredentials(rowName.value, id, secret);
		applyOauthRowMeta(row);
		staticClient.id = "";
		staticClient.secret = "";
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		savingClient.value = false;
	}
}

// Mirrors DirectSubscriptionCard's own copy button: the Clipboard API needs a
// secure context, so a plain LAN http:// deployment falls back to a
// detached-textarea execCommand copy. The URL is plain selectable text either
// way, so a failed copy still leaves the user able to select and copy by hand.
function copyRedirectUri() {
	const text = rowRedirectUri.value;
	if (!text) return;
	const done = () => {
		copied.value = true;
		setTimeout(() => {
			copied.value = false;
		}, 1400);
	};
	if (navigator.clipboard && window.isSecureContext) {
		navigator.clipboard
			.writeText(text)
			.then(done)
			.catch(() => {});
		return;
	}
	const ta = document.createElement("textarea");
	ta.value = text;
	ta.style.position = "fixed";
	ta.style.left = "-9999px";
	document.body.appendChild(ta);
	ta.focus();
	ta.select();
	try {
		if (document.execCommand("copy")) done();
	} catch (e) {
		/* best-effort - see the comment above */
	}
	document.body.removeChild(ta);
}

function onBaseUrlChange(v) {
	form.base_url = v;
}

// Custom URL's "Check" step (design §8): probes the pasted server and decides
// whether it needs a sign-in at all. needs_signin:false leaves today's token
// mode untouched; needs_signin:true switches this row into OAuth mode (the
// template's presetHasOauth branch) the same way picking GitHub does.
async function runProbe() {
	const url = form.base_url.trim();
	if (!url || probing.value) return;
	probing.value = true;
	probeError.value = "";
	try {
		const res = await probeConnectorAuth(url);
		if (res && res.ok) {
			customUrlOauth.active = !!res.needs_signin;
			customUrlOauth.signinHost = res.needs_signin ? res.signin_host || "" : "";
			customUrlOauth.registration = res.needs_signin ? res.registration || "" : "";
			form.auth_method = res.needs_signin ? "OAuth" : "API Key";
		} else {
			probeError.value =
				(res && res.error && res.error.message) || "Could not check this address.";
		}
	} catch (e) {
		probeError.value = errMessage(e, "Could not check this address.");
	} finally {
		probing.value = false;
	}
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
	// OAuth mode has no credential field - the Test connection box only ever
	// renders once rowOauthConnected is true (see template), so there's
	// nothing further to gate here.
	if (form.auth_method === "OAuth") return true;
	// open has no credential field either - it creates+tests with an empty
	// credential (runTest), same one-click shape as OAuth above.
	if (presetAuthClass.value === "open") return true;
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
				auth_method: form.auth_method,
				...(form.preset === "Custom URL"
					? { key: customUrlKey(form.base_url.trim()) }
					: {}),
			});
			rowName.value = row.name;
			createdThisSession.value = true;
		} else if (form.auth_method !== "OAuth") {
			// Re-test (edit mode, or a second Test press this session): persist
			// whatever changed first. Blank credential means "keep the saved one".
			// An OAuth row has no credential to resend - its row was already
			// created by startOauthConnect, so this branch never runs for it.
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
