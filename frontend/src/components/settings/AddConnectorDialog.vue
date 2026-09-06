<template>
	<Dialog v-model="show" :options="{ title: dialogTitle, size: 'xl' }" @after-leave="onClosed">
		<template #body-content>
			<!-- ── Step 1: connect ─────────────────────────────────────────── -->
			<div v-if="step === 1" class="flex flex-col gap-4">
				<div class="flex items-center gap-2 text-xs text-ink-gray-5">
					<span class="flex items-center gap-1.5 font-medium text-ink-gray-9">
						<span
							class="flex size-4 items-center justify-center rounded-full bg-surface-gray-7 text-[10px] font-medium text-ink-white"
						>
							1
						</span>
						Connect
					</span>
					<span class="h-px w-6 bg-outline-gray-2" />
					<span class="flex items-center gap-1.5">
						<span
							class="flex size-4 items-center justify-center rounded-full bg-surface-gray-2 text-[10px] font-medium text-ink-gray-5"
						>
							2
						</span>
						Permissions
					</span>
				</div>

				<div class="flex items-center gap-3 rounded-lg border px-3 py-2.5">
					<ConnectorLogo
						:preset="form.preset"
						:size="24"
						class="shrink-0 text-ink-gray-5"
					/>
					<div class="min-w-0 flex-1">
						<div class="truncate text-sm font-medium text-ink-gray-9">
							{{ chipName }}
						</div>
						<div v-if="chipSub" class="truncate text-xs text-ink-gray-5">
							{{ chipSub }}
						</div>
					</div>
					<button
						v-if="!isEdit"
						type="button"
						class="shrink-0 text-xs text-ink-gray-6 hover:underline"
						@click="onChange"
					>
						Change
					</button>
				</div>

				<template v-if="form.preset === 'Custom URL'">
					<div class="flex flex-col gap-2">
						<div class="flex items-end gap-2">
							<FormControl
								class="flex-1"
								type="text"
								label="Base URL"
								placeholder="https://example.com/mcp"
								:modelValue="form.base_url"
								@update:modelValue="(v) => (form.base_url = v)"
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
					</div>
				</template>

				<div v-if="isAdmin && !isEdit" class="flex items-center justify-between gap-3">
					<span class="text-xs text-ink-gray-5">Who can use it</span>
					<TabButtons
						v-if="!scopeLocked"
						:buttons="SCOPE_OPTIONS"
						:model-value="form.scope"
						@update:model-value="onScopeChange"
					/>
					<span v-else class="text-sm text-ink-gray-7">
						{{ form.scope === "Shared" ? "Everyone" : "Only me" }}
					</span>
				</div>

				<!-- One connect card, its shape picked by cardKind (see the state
				     machine note above the script). -->
				<div class="flex flex-col gap-2">
					<div class="flex flex-col gap-3 rounded-lg border p-3">
						<template v-if="cardKind === 'need-check'">
							<p class="text-xs text-ink-gray-5">
								Check the address above to continue.
							</p>
						</template>

						<template v-else-if="cardKind === 'signin'">
							<template v-if="signingIn">
								<template v-if="!rowName">
									<p class="text-xs text-ink-gray-5">Setting up…</p>
								</template>
								<template v-else>
									<p class="text-xs text-ink-gray-5">
										Finish signing in to {{ signinAppName }} in the other tab.
									</p>
								</template>
								<div class="flex justify-end">
									<Button
										variant="ghost"
										size="sm"
										label="Cancel"
										@click="cancelSignIn"
									/>
								</div>
							</template>
							<template v-else-if="rowOauthConnected">
								<template v-if="testing">
									<p class="text-xs text-ink-gray-5">Checking the connection…</p>
								</template>
								<template v-else-if="testState.status === 'passed'">
									<div
										class="flex items-center gap-1.5 text-xs text-ink-green-3"
									>
										<FeatherIcon name="check" class="size-3.5" />
										Connected · {{ testState.tools.length }}
										{{ testState.tools.length === 1 ? "action" : "actions" }}
										found
									</div>
									<div class="flex justify-end">
										<Button
											variant="ghost"
											size="sm"
											icon="log-out"
											:tooltip="'Disconnect'"
											:loading="disconnectingInline"
											@click="disconnectInline"
										/>
									</div>
								</template>
								<template v-else-if="testState.status === 'failed'">
									<p class="text-xs text-ink-red-4">{{ testState.message }}</p>
									<div class="flex justify-end">
										<Button
											variant="ghost"
											size="sm"
											icon="log-out"
											:tooltip="'Disconnect'"
											:loading="disconnectingInline"
											@click="disconnectInline"
										/>
									</div>
								</template>
							</template>
							<template v-else-if="connectError">
								<p class="text-xs text-ink-red-4">{{ connectError }}</p>
								<div class="flex justify-end">
									<Button
										variant="solid"
										label="Try again"
										iconRight="external-link"
										@click="beginSignIn"
									/>
								</div>
							</template>
							<template v-else>
								<div>
									<div class="text-sm font-medium text-ink-gray-9">
										Sign in to {{ signinAppName }}
									</div>
									<p class="mt-1 text-p-sm text-ink-gray-6">
										You will be sent to
										{{ displaySigninHost || "the provider" }} to approve
										access.
									</p>
								</div>
								<div class="flex justify-end">
									<Button
										variant="solid"
										:label="`Sign in with ${signinAppName}`"
										iconRight="external-link"
										@click="beginSignIn"
									/>
								</div>
							</template>
						</template>

						<template v-else-if="cardKind === 'register'">
							<div>
								<div class="text-sm font-medium text-ink-gray-9">
									Register your {{ form.preset }} app once
								</div>
								<p v-if="staticHint" class="mt-1 text-p-sm text-ink-gray-6">
									{{ staticHint }}
								</p>
							</div>

							<div class="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1.5">
								<span
									class="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-surface-gray-2 text-2xs font-medium text-ink-gray-6"
									>1</span
								>
								<div class="text-sm text-ink-gray-9">
									Copy this callback address into the app
								</div>
								<div
									v-if="registerRedirectUri"
									class="col-start-2 flex items-center gap-2"
								>
									<code
										:title="registerRedirectUri"
										class="min-w-0 flex-1 truncate rounded border px-2 py-1 font-mono text-xs text-ink-gray-7"
										>{{ registerRedirectUri }}</code
									>
									<Button
										variant="ghost"
										icon="copy"
										:tooltip="copied ? 'Copied' : 'Copy'"
										class="shrink-0"
										@click="copyRedirectUri"
									/>
								</div>
							</div>

							<div class="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1.5">
								<span
									class="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-surface-gray-2 text-2xs font-medium text-ink-gray-6"
									>2</span
								>
								<div class="text-sm text-ink-gray-9">
									Create the app in your {{ form.preset }} organisation
								</div>
								<a
									v-if="staticHelpUrl"
									:href="staticHelpUrl"
									target="_blank"
									rel="noopener"
									class="col-start-2 inline-flex w-fit items-center gap-1 text-xs text-ink-blue-link hover:underline"
								>
									Open {{ form.preset }} app settings
									<FeatherIcon name="external-link" class="size-3" />
								</a>
							</div>

							<div class="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1.5">
								<span
									class="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-surface-gray-2 text-2xs font-medium text-ink-gray-6"
									>3</span
								>
								<div class="text-sm text-ink-gray-9">
									Paste what {{ form.preset }} gives you
								</div>
								<div class="col-start-2 grid grid-cols-2 gap-2">
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
								</div>
							</div>

							<div class="flex justify-end">
								<Button
									variant="solid"
									label="Save"
									:loading="savingClient"
									:disabled="
										!staticClient.id.trim() || !staticClient.secret.trim()
									"
									@click="saveStaticClient"
								/>
							</div>
							<p v-if="connectError" class="text-xs text-ink-red-4">
								{{ connectError }}
							</p>
						</template>

						<template v-else-if="cardKind === 'ask-admin'">
							<p class="text-xs text-ink-gray-5">Ask your admin to finish setup.</p>
						</template>

						<template v-else-if="cardKind === 'key'">
							<div>
								<div class="text-sm font-medium text-ink-gray-9">Paste a key</div>
								<p v-if="tokenHint" class="mt-1 text-p-sm text-ink-gray-6">
									{{ tokenHint }}
								</p>
							</div>
							<FormControl
								type="password"
								label="Key"
								:placeholder="
									isEdit ? 'Leave blank to keep the saved key' : 'Paste your key'
								"
								:modelValue="form.credential"
								@update:modelValue="onCredentialChange"
							/>
							<div class="flex items-center justify-between gap-2">
								<a
									v-if="tokenDocsUrl"
									:href="tokenDocsUrl"
									target="_blank"
									rel="noopener"
									class="inline-flex items-center gap-1 text-xs text-ink-blue-link hover:underline"
								>
									How to create this key
									<FeatherIcon name="external-link" class="size-3" />
								</a>
								<span v-else />
								<Button
									variant="solid"
									:label="testing ? 'Connecting…' : 'Connect'"
									:loading="testing"
									:disabled="!canConnectKey"
									@click="runConnect"
								/>
							</div>
							<div
								v-if="testState.status === 'passed'"
								class="flex items-center gap-1.5 text-xs text-ink-green-3"
							>
								<FeatherIcon name="check" class="size-3.5" />
								Connected · {{ testState.tools.length }}
								{{ testState.tools.length === 1 ? "action" : "actions" }} found
							</div>
							<p
								v-else-if="testState.status === 'failed'"
								class="text-xs text-ink-red-4"
							>
								{{ testState.message }}
							</p>
						</template>

						<template v-else-if="cardKind === 'open'">
							<p class="text-xs text-ink-gray-5">No sign-in needed.</p>
							<div class="flex justify-end">
								<Button
									variant="solid"
									:label="testing ? 'Connecting…' : 'Connect'"
									:loading="testing"
									@click="runConnect"
								/>
							</div>
							<div
								v-if="testState.status === 'passed'"
								class="flex items-center gap-1.5 text-xs text-ink-green-3"
							>
								<FeatherIcon name="check" class="size-3.5" />
								Connected · {{ testState.tools.length }}
								{{ testState.tools.length === 1 ? "action" : "actions" }} found
							</div>
							<p
								v-else-if="testState.status === 'failed'"
								class="text-xs text-ink-red-4"
							>
								{{ testState.message }}
							</p>
						</template>
					</div>
					<div
						v-if="showUseKeyInsteadLink || showSignInInsteadLink"
						class="flex justify-end"
					>
						<Button
							v-if="showUseKeyInsteadLink"
							variant="ghost"
							size="sm"
							label="Use a key instead"
							class="text-ink-gray-5"
							@click="switchAuthMethod('API Key')"
						/>
						<Button
							v-if="showSignInInsteadLink"
							variant="ghost"
							size="sm"
							:label="`Sign in with ${form.preset} instead`"
							class="text-ink-gray-5"
							@click="switchAuthMethod('OAuth')"
						/>
					</div>
				</div>
			</div>

			<!-- ── Step 2: allowed actions ─────────────────────────────────── -->
			<div v-else class="flex flex-col gap-3">
				<div class="flex items-center gap-3 rounded-lg border px-3 py-2.5">
					<ConnectorLogo
						:preset="form.preset"
						:size="24"
						class="shrink-0 text-ink-gray-5"
					/>
					<div class="min-w-0 flex-1">
						<div class="truncate text-sm font-medium text-ink-gray-9">
							{{ chipName }}
						</div>
						<div v-if="chipSub" class="truncate text-xs text-ink-gray-5">
							{{ chipSub }}
						</div>
					</div>
				</div>

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
						<div
							class="mb-1 text-xs font-medium uppercase tracking-wide text-ink-gray-5"
						>
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
						<div
							class="mb-1 text-xs font-medium uppercase tracking-wide text-ink-gray-5"
						>
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
				<Button
					v-if="step === 2"
					label="Back"
					:disabled="saving"
					class="mr-auto"
					@click="step = 1"
				/>
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
// Add/edit an MCP connector (Option B, chosen 2026-09-06 - see build.py's
// note-b). The app is always picked on the Browse tab first
// (ConnectorDirectory); this dialog opens straight onto that preset (or
// "Custom URL"), no in-dialog Select any more.
//
// ── State machine (six connect-card shapes, one at a time) ──────────────────
//   cardKind:
//     "need-check" - Custom URL, Check not run yet. No action; Continue stays
//         disabled (testState never reaches "passed" from here).
//     "signin"     - dcr, or static once a client is on file, or Custom URL
//         once Check found needs_signin. Sub-states: signingIn (itself either
//         "creating the row" or "waiting on the other tab", see signingIn +
//         rowName in the template) -> (rowOauthConnected -> testing ->
//         passed/failed) | connectError.
//     "register"   - static, no client on file yet, current user allowed to
//         set one. Numbered mini-steps; "Save" creates the row first if none
//         exists yet, then persists the client, and the card flips to
//         "signin" on the next render.
//     "ask-admin"  - static, no client, current user NOT allowed to set one.
//     "key"        - token preset, or Custom URL once Check found no sign-in
//         needed, or any sign-in preset the user explicitly switched off of.
//     "open"       - open preset (no credential, no sign-in).
//
// No row is ever created just from opening this dialog or from touching the
// form (scope, auth method, Custom URL's Check) - only an actual connect
// press does: "Sign in with X" (via a factory `signIn()` runs AFTER it opens
// the vendor tab - see oauthSignin.js), "Save" on the register card, or
// "Connect" on the key/open card. Every one of those creates the row with
// enabled:0 (F8) - nobody but this dialog's owner can see it until the final
// Save in step 2 flips it to enabled:1.
//
// isPlaceholder is the one predicate that reconciles every tension the spec
// itself narrates: a row THIS dialog created that nobody has invested in yet
// (no saved client, no live sign-in, no passing test) is disposable. It
// drives three things at once: the Cancel-rule cleanup below, whether "Who
// can use it" is still a live toggle or already plain text, and whether the
// "use a key/sign in instead" switch links still show. Switching the auth
// method away from a placeholder row still discards it (auth_method is
// immutable server side, so a fresh row is what a later press will create);
// switching scope can no longer discard-and-recreate anything because the
// toggle isn't even rendered once a row exists - see scopeLocked below.
import { computed, reactive, ref, watch } from "vue";
import {
	Badge,
	Button,
	Dialog,
	FeatherIcon,
	FormControl,
	Switch,
	TabButtons,
	toast,
} from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";
import { CUSTOM_URL_TOKEN_HINT } from "@/components/settings/connectorHelp.js";
// oauthSignin.js's signIn() - opens the vendor tab synchronously and returns
// a promise (with a .cancel()) that resolves once the flow has a verdict.
import { signIn } from "@/components/settings/oauthSignin";
import {
	addConnector,
	deleteConnector,
	disconnectOauth,
	oauthSigninStatus,
	probeConnectorAuth,
	setConnectorAllowedActions,
	setOauthClientCredentials,
	testConnector,
	updateConnector,
} from "@/api";
import { agentName } from "@/branding";
import { errMessage, errHtml } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// "Shared" or "Personal" - the default scope handed in from the pane (an
	// admin's Browse press defaults Shared; a non-admin's is always Personal).
	// Ignored in edit mode.
	scope: { type: String, default: "Personal" },
	// The catalog name chosen on Browse, or "Custom URL". Ignored in edit mode.
	preset: { type: String, default: "" },
	allowCustomUrls: { type: Boolean, default: true },
	// listConnectors()'s site-wide oauth_redirect_uri - the register card's step 1
	// needs this BEFORE any row exists (a static preset's register card can show
	// first, on the very first render, with no connector created yet); once a row
	// exists its own oauth status carries the same value and takes over.
	redirectUri: { type: String, default: "" },
	// The row being edited, or null for a fresh Add.
	connector: { type: Object, default: null },
	// listConnectors()'s catalog: [{ name, key, auth, category, description,
	// logo, help_url, hint, token_hint, token_help_url }], enabled providers,
	// catalog order.
	catalog: { type: Array, default: () => [] },
});
// "kept" - a row this dialog created is still around (oauth-connected) when
// the dialog closes without going through save() - the pane reloads so it
// shows up on Installed (F8/F6).
const emit = defineEmits(["update:modelValue", "saved", "change", "kept"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const SCOPE_OPTIONS = [
	{ label: "Everyone", value: "Shared" },
	{ label: "Only me", value: "Personal" },
];

const isAdmin = !!window.is_system_manager || !!window.is_jarvis_admin;
// Who may enter an app's client id/secret: an admin for a Shared row, or the
// owner of their own Personal row (their app, their connector).
const canSetStaticClient = computed(
	() => isAdmin || (isEdit.value ? props.connector.scope : form.scope) === "Personal"
);

const isEdit = computed(() => !!props.connector);
const dialogTitle = computed(() => {
	if (isEdit.value) return "Edit connector";
	return form.preset === "Custom URL" ? "Add a custom server" : `Connect ${form.preset}`;
});

const step = ref(1);
const saving = ref(false);
const testing = ref(false);

const form = reactive({
	preset: "",
	base_url: "",
	credential: "",
	auth_method: "API Key",
	scope: "Personal",
});

function catalogAuthOf(name) {
	if (name === "Custom URL") return null;
	const entry = props.catalog.find((c) => c.name === name);
	return entry ? entry.auth : null;
}
const catalogEntry = computed(() => props.catalog.find((c) => c.name === form.preset) || null);
// dcr/static both default to a sign-in flow; token/open never do.
function presetDefaultsToOauth(auth) {
	return auth === "dcr" || auth === "static";
}
const presetAuthClass = computed(() => {
	if (isEdit.value) return props.connector.auth_class;
	return form.preset === "Custom URL" ? "custom" : catalogAuthOf(form.preset);
});
// Whether the selected preset offers a sign-in option at all. Custom URL only
// joins this once Check finds the pasted server needs one.
const presetHasOauth = computed(() => {
	if (form.preset === "Custom URL") return customUrlOauth.active;
	return presetDefaultsToOauth(presetAuthClass.value);
});

// ── the one saved (or placeholder) row this dialog is working against ──────
const rowName = ref("");
// Only true for a row THIS dialog session created (never for an edited row).
const createdThisSession = ref(false);
const savedClientThisSession = ref(false);
const rowOauthConnected = ref(false);
const rowNeedsStaticClient = ref(false);
const rowRedirectUri = ref("");
// Step 1's callback address for the register card: the row's own value once one
// exists (applyOauthRowMeta), else the site-wide value list_connectors shipped
// (props.redirectUri) - same address either way (connectors_api.oauth_redirect_uri
// is the ONE value both come from), this only decides which is on hand first so
// the register card can show the callback before any row has been created.
const registerRedirectUri = computed(() => rowRedirectUri.value || props.redirectUri);
// The known sign-in host, once a row exists (any preset - set from the row's
// own signin_host, same field a Custom URL's pre-row Check already surfaces
// into customUrlOauth.signinHost below).
const signinHost = ref("");
const testState = reactive({ status: "idle", tools: [], message: "" }); // idle | passed | failed

// A row THIS session created that nobody has invested in yet - see the state
// machine note above. This is also, verbatim, the Cancel-rule predicate.
const isPlaceholder = computed(
	() =>
		createdThisSession.value &&
		!rowOauthConnected.value &&
		!savedClientThisSession.value &&
		testState.status !== "passed"
);
// "Who can use it" only stays a live toggle while no row exists yet - once
// one does (even a placeholder), scope is locked, never discarded-and-redone.
const scopeLocked = computed(() => !!rowName.value);

const signingIn = ref(false);
// Set the moment "Sign in with X" is first pressed this dialog session -
// onClosed() uses it to decide whether a final status check is worth making
// before deleting an unfinished row (F6: a token can land just after the
// poll gives up, and a plain close must not race it away).
const signInStartedThisSession = ref(false);
// The in-flight signIn() promise, if any - cancelSignIn() and onClosed() both
// call its .cancel() (see oauthSignin.js's module doc for the contract).
let currentSignIn = null;
// Bumped on every session-changing event (sign-in start/cancel, dialog
// close, dialog reopen) so any async call this dialog has in flight -
// signIn(), or a row-creation/test await in saveStaticClient(), runConnect()
// or onClosed() - can tell once it resolves whether the user has since moved
// on (Cancel, Escape/close, Change, or a reopen for a different preset) and
// back out instead of mutating a row or card that isn't this session's any
// more. The dialog instance persists across close/reopen (see the module
// doc above), so this is the only thing that tells one session from the next.
let sessionGen = 0;
const connectError = ref("");
const disconnectingInline = ref(false);
// Set in save()'s success path so onClosed doesn't fire a second "kept"
// reload on top of the "saved" one it already emitted (Save closes the
// dialog too, so onClosed still runs via @after-leave).
const savedThisClose = ref(false);

// Custom URL's Check step: probes the pasted server and decides whether it
// needs a sign-in at all.
const customUrlOauth = reactive({ active: false, signinHost: "", registration: "" });
const probing = ref(false);
const probeError = ref("");
const probeDone = ref(false);

const staticClient = reactive({ id: "", secret: "" });
const savingClient = ref(false);
const copied = ref(false);

function resetCustomUrlOauthState() {
	customUrlOauth.active = false;
	customUrlOauth.signinHost = "";
	customUrlOauth.registration = "";
	probing.value = false;
	probeError.value = "";
	probeDone.value = false;
	rowNeedsStaticClient.value = false;
	rowRedirectUri.value = "";
	staticClient.id = "";
	staticClient.secret = "";
	savingClient.value = false;
	copied.value = false;
	connectError.value = "";
}

function defaultPreset() {
	if (props.catalog.length) return props.catalog[0].name;
	return props.allowCustomUrls ? "Custom URL" : "";
}

// ── the connect card this render shows ──────────────────────────────────────
const cardKind = computed(() => {
	if (isEdit.value) {
		const auth = presetAuthClass.value;
		// F3: a sign-in Custom URL row's auth_class is "custom", same as any
		// other Custom URL row - only auth_method distinguishes it, so it must
		// be checked here too, not just auth === "dcr".
		if (auth === "dcr" || (auth === "custom" && form.auth_method === "OAuth")) return "signin";
		// F2: rowNeedsStaticClient (seeded from the row in resetForEdit), not
		// props.connector.needs_static_client - the latter is a snapshot from
		// dialog-open and never updates after saveStaticClient() writes the
		// client, so Save never left the register card.
		if (auth === "static")
			return rowNeedsStaticClient.value
				? canSetStaticClient.value
					? "register"
					: "ask-admin"
				: "signin";
		if (auth === "open") return "open";
		return "key"; // token, or a Custom URL row (any auth_method not caught above)
	}
	if (form.preset === "Custom URL") {
		if (!probeDone.value) return "need-check";
		return customUrlOauth.active ? "signin" : "key";
	}
	const auth = catalogAuthOf(form.preset);
	if (form.auth_method === "API Key") return auth === "open" ? "open" : "key";
	if (auth === "dcr") return "signin";
	// No row exists yet, so the row's own needs_static_client (still its
	// reset-state false) cannot answer this - a bring-your-own-app static preset
	// always needs one, so show "register" (or "ask-admin") from the very first
	// render instead of a "Sign in with X" that would create the row, fail server
	// side for want of app credentials, and only then flip.
	if (auth === "static")
		return !rowName.value || rowNeedsStaticClient.value
			? canSetStaticClient.value
				? "register"
				: "ask-admin"
			: "signin";
	if (auth === "open") return "open";
	return "key";
});

// "Use a key instead" / "Sign in with X instead": only while the row (if any)
// backing the current shape is still a placeholder, never once something is
// actually saved on it - see the module doc above.
const showUseKeyInsteadLink = computed(
	() =>
		!isEdit.value &&
		(cardKind.value === "signin" || cardKind.value === "register") &&
		(!rowName.value || isPlaceholder.value)
);
const showSignInInsteadLink = computed(
	() =>
		!isEdit.value &&
		cardKind.value === "key" &&
		presetHasOauth.value &&
		(!rowName.value || isPlaceholder.value)
);

const signinAppName = computed(() =>
	form.preset === "Custom URL" ? customUrlOauth.signinHost || "this server" : form.preset
);
const displaySigninHost = computed(() => signinHost.value || customUrlOauth.signinHost || "");

const connectorDisplayName = computed(() => {
	if (isEdit.value && props.connector?.label) return props.connector.label;
	if (form.preset && form.preset !== "Custom URL") return form.preset;
	return "this connector";
});
const chipName = computed(() => (form.preset === "Custom URL" ? "Custom URL" : form.preset));
const chipSub = computed(() => {
	if (form.preset === "Custom URL") {
		return customUrlOauth.signinHost
			? `Signs in at ${customUrlOauth.signinHost}`
			: "Paste your own server address";
	}
	return catalogEntry.value?.description || "";
});

const tokenHint = computed(() => {
	if (form.preset === "Custom URL") return CUSTOM_URL_TOKEN_HINT;
	const entry = catalogEntry.value;
	if (!entry) return "";
	if (entry.token_hint) return entry.token_hint;
	return entry.auth !== "static" ? entry.hint || "" : "";
});
const tokenDocsUrl = computed(() => {
	if (form.preset === "Custom URL") return "";
	const entry = catalogEntry.value;
	if (!entry) return "";
	if (entry.token_help_url) return entry.token_help_url;
	return entry.auth !== "static" ? entry.help_url || "" : "";
});
const staticHint = computed(() => {
	if (form.preset === "Custom URL") return "";
	return catalogEntry.value?.hint || "";
});
const staticHelpUrl = computed(() => {
	if (form.preset === "Custom URL") return "";
	return catalogEntry.value?.help_url || "";
});

// ── reset on open ───────────────────────────────────────────────────────────
function resetForCreate() {
	form.preset = props.preset || defaultPreset();
	form.base_url = "";
	form.credential = "";
	form.scope = isAdmin ? (props.scope === "Personal" ? "Personal" : "Shared") : "Personal";
	form.auth_method = presetDefaultsToOauth(catalogAuthOf(form.preset)) ? "OAuth" : "API Key";
	rowName.value = "";
	createdThisSession.value = false;
	savedClientThisSession.value = false;
	rowOauthConnected.value = false;
	signinHost.value = "";
	signingIn.value = false;
	signInStartedThisSession.value = false;
	savedThisClose.value = false;
	connectError.value = "";
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
	form.scope = row.scope || "Personal";
	rowName.value = row.name;
	createdThisSession.value = false;
	savedClientThisSession.value = false;
	rowOauthConnected.value = !!row.oauth_connected;
	signinHost.value = row.signin_host || "";
	signingIn.value = false;
	signInStartedThisSession.value = false;
	savedThisClose.value = false;
	connectError.value = "";
	// An edited row may already have a passing test on record, but this dialog
	// only knows the LIVE tools/list shape after a fresh test - it starts idle
	// and asks for a re-test before Continue unlocks (except the oauth-connected
	// case below, whose card has no manual re-test action other than Disconnect).
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	step.value = 1;
	selected.value = {};
	touchedActions.value = new Set();
	actionQuery.value = "";
	resetCustomUrlOauthState();
	if (row.preset === "Custom URL" && row.auth_method === "OAuth") {
		customUrlOauth.active = true;
		customUrlOauth.signinHost = row.signin_host || "";
		probeDone.value = true;
	}
	rowNeedsStaticClient.value = !!row.needs_static_client;
	rowRedirectUri.value = row.oauth_redirect_uri || "";
	if (rowOauthConnected.value) runOauthTest();
}

watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		sessionGen++; // invalidate any stale in-flight session from before this open
		if (props.connector) resetForEdit(props.connector);
		else resetForCreate();
	}
);

watch([() => form.preset, () => form.base_url], () => {
	if (testState.status !== "idle") {
		testState.status = "idle";
		testState.tools = [];
		testState.message = "";
	}
});
watch(
	() => form.base_url,
	async () => {
		if (rowName.value) {
			if (!isPlaceholder.value) return;
			await discardPlaceholderRow();
		}
		if (!customUrlOauth.active && !probeError.value && !probeDone.value) return;
		resetCustomUrlOauthState();
		if (form.preset === "Custom URL") form.auth_method = "API Key";
	}
);

// ── who-can-use-it / auth-method switches (may discard a placeholder row) ───
async function discardPlaceholderRow() {
	const name = rowName.value;
	if (!name) return;
	rowName.value = "";
	createdThisSession.value = false;
	rowOauthConnected.value = false;
	rowNeedsStaticClient.value = false;
	rowRedirectUri.value = "";
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	connectError.value = "";
	try {
		await deleteConnector(name);
	} catch (e) {
		/* best-effort cleanup */
	}
}
// With no row yet, flipping "who can use it" is just a form change - nothing
// to discard (the toggle isn't even rendered once a row exists, see
// scopeLocked above).
function onScopeChange(v) {
	if (!v || v === form.scope) return;
	form.scope = v;
}
// A placeholder row's auth_method is set once, server side - switching away
// from it (via the "use a key/sign in instead" links) discards it rather
// than trying to mutate it. With no row yet this is just a form change; a
// later connect press creates the row that matches the new choice.
async function switchAuthMethod(method) {
	if (method === form.auth_method) return;
	form.auth_method = method;
	form.credential = "";
	connectError.value = "";
	testState.status = "idle";
	testState.tools = [];
	testState.message = "";
	if (rowName.value && isPlaceholder.value) await discardPlaceholderRow();
}

function customUrlKey(url) {
	try {
		return slugifyKey(new URL(url).hostname);
	} catch (e) {
		return slugifyKey(url);
	}
}
function slugifyKey(text) {
	const slug = (text || "")
		.toLowerCase()
		.replace(/[^a-z0-9_-]+/g, "-")
		.replace(/^-+/, "")
		.slice(0, 64);
	return slug || "custom";
}
function applyOauthRowMeta(row) {
	if (!row) return;
	rowOauthConnected.value = !!row.oauth_connected;
	rowNeedsStaticClient.value = !!row.needs_static_client;
	rowRedirectUri.value = row.oauth_redirect_uri || "";
	if (row.signin_host) signinHost.value = row.signin_host;
}
// Shared shape for every "create the row this connect press needs" call -
// always enabled:0 (F8): a row nobody has finished setting up yet stays
// invisible to everyone else until the final Save (step 2) turns it on.
function connectRowPayload(extra) {
	return {
		preset: form.preset,
		scope: form.scope,
		enabled: 0,
		...(form.preset === "Custom URL"
			? { base_url: form.base_url.trim(), key: customUrlKey(form.base_url.trim()) }
			: {}),
		...extra,
	};
}

// ── sign-in ──────────────────────────────────────────────────────────────
const SIGNIN_STATUS_MESSAGE = {
	closed: "Sign-in window was closed.",
	timeout: "Sign-in took too long.",
	error: "Could not sign in.",
};
// signIn()'s factory (F4): runs AFTER the vendor tab is open and BEFORE
// connect_oauth, so the row is only created once the user has actually
// pressed "Sign in with X". A static preset with no client on file yet can't
// sign in at all - see the register card - so that's surfaced as a thrown
// error, which signIn() turns into a closed tab + {status:"error"} that the
// (now "register") card's own connectError line shows.
// `gen` is beginSignIn's own sessionGen snapshot - Cancel, Escape/close and a
// dialog reopen all bump sessionGen, so checking it after the create's await
// catches every way the user could have moved on during the ~45s a discovery
// + registration can take. runConnect and saveStaticClient guard their own
// row-creation awaits the same way, snapshotting sessionGen before the await
// and comparing after.
async function createRowForSignIn(gen) {
	const row = await addConnector(connectRowPayload({ auth_method: "OAuth" }));
	if (gen !== sessionGen) {
		deleteConnector(row.name).catch(() => {});
		throw new Error("Could not sign in.");
	}
	rowName.value = row.name;
	createdThisSession.value = true;
	applyOauthRowMeta(row);
	if (row.needs_static_client) throw new Error("Register your app first.");
	return row.name;
}
async function beginSignIn() {
	if (signingIn.value) return;
	const gen = ++sessionGen;
	signingIn.value = true;
	signInStartedThisSession.value = true;
	connectError.value = "";
	// A row already on hand (a retry, or one "Sign in with X" already created)
	// signs in directly by name; otherwise the factory above creates it -
	// signIn() calls it synchronously in this same click, after opening the
	// tab, per the interface contract.
	const target = rowName.value || (() => createRowForSignIn(gen));
	const pending = signIn(target, { label: signinAppName.value, agentName });
	currentSignIn = pending;
	try {
		const res = await pending;
		if (gen !== sessionGen) return; // superseded by Cancel / a later open
		if (res && res.status === "connected") {
			rowOauthConnected.value = true;
			await runOauthTest();
		} else if (res && res.status === "navigated") {
			// The whole page is leaving - nothing to do.
		} else {
			connectError.value =
				(res && res.message) ||
				SIGNIN_STATUS_MESSAGE[res && res.status] ||
				"Could not sign in.";
		}
	} catch (e) {
		if (gen !== sessionGen) return;
		connectError.value = errMessage(e, "Could not sign in.");
	} finally {
		if (gen === sessionGen) signingIn.value = false;
		if (currentSignIn === pending) currentSignIn = null;
	}
}
function cancelSignIn() {
	sessionGen++; // the pending signIn() promise, whenever it settles, is now stale
	signingIn.value = false;
	if (currentSignIn) currentSignIn.cancel();
}
async function runOauthTest() {
	if (!rowName.value) return;
	testing.value = true;
	try {
		applyTestResult(await testConnector(rowName.value));
	} catch (e) {
		testState.status = "failed";
		testState.tools = [];
		testState.message = errMessage(e);
	} finally {
		testing.value = false;
	}
}
async function disconnectInline() {
	if (!rowName.value || disconnectingInline.value) return;
	disconnectingInline.value = true;
	try {
		await disconnectOauth(rowName.value);
		rowOauthConnected.value = false;
		testState.status = "idle";
		testState.tools = [];
		testState.message = "";
	} catch (e) {
		connectError.value = errMessage(e, "Could not disconnect.");
	} finally {
		disconnectingInline.value = false;
	}
}

// ── register-your-app ────────────────────────────────────────────────────
// F4/F8: creates the row first when this dialog doesn't have one yet - the
// primary path for a fresh bring-your-own-app static preset, whose card is
// "register" from the very first render (see cardKind), no prior "Sign in
// with X" press required. Also covers a row a "Sign in with X" press already
// created and found needs_static_client on (createRowForSignIn) and an
// edit-mode row that never got a client - then saves the pasted credentials,
// one press, one handler.
async function saveStaticClient() {
	if (savingClient.value) return;
	const id = staticClient.id.trim();
	const secret = staticClient.secret.trim();
	if (!id || !secret) return;
	// Snapshot before the awaits below - Cancel/Escape/reopen bump sessionGen,
	// so a create or save that resolves after the user has moved on to a
	// different preset can tell and back out instead of binding to it.
	const gen = sessionGen;
	savingClient.value = true;
	connectError.value = "";
	try {
		if (!rowName.value) {
			const row = await addConnector(connectRowPayload({ auth_method: "OAuth" }));
			if (gen !== sessionGen) {
				deleteConnector(row.name).catch(() => {});
				return;
			}
			rowName.value = row.name;
			createdThisSession.value = true;
			applyOauthRowMeta(row);
		}
		const saved = await setOauthClientCredentials(rowName.value, id, secret);
		if (gen !== sessionGen) return; // stale - rowName above is no longer this session's
		applyOauthRowMeta(saved);
		savedClientThisSession.value = true;
		staticClient.id = "";
		staticClient.secret = "";
	} catch (e) {
		if (gen !== sessionGen) return; // stale - don't surface an error onto a moved-on session
		connectError.value = errMessage(e, "Could not save these details.");
	} finally {
		savingClient.value = false;
	}
}
function copyRedirectUri() {
	const text = registerRedirectUri.value;
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

// ── Custom URL Check ─────────────────────────────────────────────────────
// Only probes - creates nothing. A needs_signin result just flips the card
// to "signin"; the row itself is created by that card's own sign-in press
// (F4), same as every other preset.
async function runProbe() {
	const url = form.base_url.trim();
	if (!url || probing.value) return;
	if (rowName.value && isPlaceholder.value) await discardPlaceholderRow();
	probing.value = true;
	probeError.value = "";
	try {
		const res = await probeConnectorAuth(url);
		if (res && res.ok) {
			probeDone.value = true;
			customUrlOauth.active = !!res.needs_signin;
			customUrlOauth.signinHost = res.needs_signin ? res.signin_host || "" : "";
			customUrlOauth.registration = res.needs_signin ? res.registration || "" : "";
			form.auth_method = res.needs_signin ? "OAuth" : "API Key";
		} else {
			probeDone.value = false;
			probeError.value =
				(res && res.error && res.error.message) || "Could not check this address.";
		}
	} catch (e) {
		probeDone.value = false;
		probeError.value = errMessage(e, "Could not check this address.");
	} finally {
		probing.value = false;
	}
}

// ── key / open connect ───────────────────────────────────────────────────
function onCredentialChange(v) {
	form.credential = v;
	if (testState.status !== "idle") {
		testState.status = "idle";
		testState.tools = [];
		testState.message = "";
	}
}
const canConnectKey = computed(() => {
	if (isEdit.value || rowName.value) return true; // re-test: blank credential keeps the saved one
	return !!form.credential.trim();
});
function applyTestResult(res) {
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
}
// F8: this is the key/open card's own connect press, so it creates the row
// (enabled:0) itself rather than relying on anything eager.
async function runConnect() {
	if (testing.value) return;
	// Snapshot before the awaits below - see saveStaticClient's comment.
	const gen = sessionGen;
	testing.value = true;
	try {
		if (!rowName.value) {
			const row = await addConnector(
				connectRowPayload({ credential: form.credential, auth_method: "API Key" })
			);
			if (gen !== sessionGen) {
				deleteConnector(row.name).catch(() => {});
				return;
			}
			rowName.value = row.name;
			createdThisSession.value = true;
		} else {
			const patch = {};
			if (form.preset === "Custom URL") patch.base_url = form.base_url.trim();
			if (form.credential.trim()) patch.credential = form.credential.trim();
			if (Object.keys(patch).length) await updateConnector(rowName.value, patch);
		}
		if (gen !== sessionGen) return; // stale - don't run/apply the test against a moved-on session
		applyTestResult(await testConnector(rowName.value));
	} catch (e) {
		if (gen !== sessionGen) return; // stale - don't surface an error onto a moved-on session
		testState.status = "failed";
		testState.tools = [];
		testState.message = errMessage(e);
	} finally {
		testing.value = false;
	}
}

// ── step 2: allowed actions (unchanged) ─────────────────────────────────────
const selected = ref({});
const touchedActions = ref(new Set());
const actionQuery = ref("");

watch(
	() => testState.tools,
	(tools) => {
		const next = {};
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
		// F8: the row was created enabled:0 (or already was, in edit mode's case
		// simply staying enabled) - this is what makes it visible to everyone
		// else the row's scope allows.
		const row = await updateConnector(rowName.value, { enabled: 1 });
		toast.success(isEdit.value ? "Connector updated" : "Connector added");
		savedThisClose.value = true; // onClosed's own reload would be redundant
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
// The app chip's "Change" link - go back to Browse instead of leaving the
// dialog pinned to a preset the user wants to swap out.
function onChange() {
	show.value = false;
	emit("change");
}

// Fires on every close - Cancel, the dialog's own X, Escape, a backdrop click
// and Save (which sets show.value itself) all land here via @after-leave.
// Only a still-placeholder row is an orphan worth cleaning up - see the
// module doc's isPlaceholder note. A sign-in still in flight is cancelled
// FIRST (closes the vendor tab, stops the poll) so it can't land a token into
// a row this function is about to delete out from under it (F6).
async function onClosed() {
	// Bump first - this both invalidates any signIn() still pending from a
	// previous open (same as cancelSignIn) and gives this close its own
	// generation: `gen` stops matching sessionGen the moment anything (a
	// reopen, another close) moves the session on while the awaits below are
	// in flight.
	const gen = ++sessionGen;
	if (currentSignIn) currentSignIn.cancel();
	// Snapshot rowName and everything isPlaceholder reads, plus what the
	// branches below need - a reopen during the awaits resets/reassigns all
	// of these (resetForCreate/resetForEdit), so reading the live refs
	// afterwards would describe whatever session is on screen now, not the
	// one this close is actually for.
	const name = rowName.value;
	const wasCreatedThisSession = createdThisSession.value;
	const wasSavedClientThisSession = savedClientThisSession.value;
	const wasRowOauthConnected = rowOauthConnected.value;
	const hadPassedTest = testState.status === "passed";
	const wasSavedThisClose = savedThisClose.value;
	const hadSignInStarted = signInStartedThisSession.value;
	const wasPlaceholder =
		wasCreatedThisSession &&
		!wasRowOauthConnected &&
		!wasSavedClientThisSession &&
		!hadPassedTest;

	let keep = !wasPlaceholder && wasRowOauthConnected;
	if (wasPlaceholder && name) {
		if (hadSignInStarted) {
			// The poll may have given up (timeout) a moment before a token
			// actually landed - one last check before writing the row off.
			try {
				const s = await oauthSigninStatus(name);
				keep = !!(s && s.ok !== false && s.connected);
			} catch (e) {
				/* best-effort - fall through to delete on a failed check */
			}
		}
		if (keep) {
			// Only touch the live ref if nothing reopened while we awaited -
			// otherwise it belongs to whatever session is showing now.
			if (gen === sessionGen) rowOauthConnected.value = true;
		} else {
			// Safe regardless of a reopen: this targets the snapshotted row
			// by name, not the live rowName ref.
			try {
				await deleteConnector(name);
			} catch (e) {
				/* best-effort cleanup */
			}
		}
	}
	// A reopen has moved the session on since this close started - anything
	// below only makes sense for the session that was actually closing, so
	// stop here. The new session's own reset already covers
	// testState/selected/touchedActions, and its own eventual close decides
	// "kept" for itself.
	if (gen !== sessionGen) return;
	// F8: a row this dialog created is still around, connected, but was never
	// saved (Cancel/close after a sign-in, or the late-arriving token just
	// above) - the pane's own lists don't know about it yet, so ask for a
	// reload. Skipped after a real Save, which already asked via "saved".
	if (!wasSavedThisClose && wasCreatedThisSession && keep) {
		emit("kept");
	}
	testState.status = "idle";
	testState.tools = [];
	selected.value = {};
	touchedActions.value = new Set();
}
</script>
