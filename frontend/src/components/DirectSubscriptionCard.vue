<template>
	<!-- DIRECT single chat-subscription re-authorize / disconnect card.
       Shown by AccountView when get_direct_subscription_status reports a tenant
       on the legacy flat-field OAuth path (empty models[], auth_mode oauth/
       subscription). These tenants are served by the container's
       auth-profiles.json, NOT the pooled cliproxy sidecar, so we re-authorize
       via the DIRECT begin/complete_paste_signin flow (which pushes a fresh
       blob + rewrites the flat fields) rather than routing them through the
       pool editor. Ports the desk account page's Screen 2/3 paste-back UI. -->
	<div class="jv-dsub" style="font-family: inherit; color: var(--text)">
		<!-- ===== Paste-back flow (Screen 2) ===== -->
		<div v-if="flowOpen">
			<p class="jv-dsub-step">
				<b>1.</b> Sign in with your {{ status.provider || pickProvider }} account in a new
				tab.
			</p>
			<div class="jv-dsub-actions" style="margin-bottom: 10px">
				<a
					v-if="authorizeUrl"
					:href="authorizeUrl"
					target="_blank"
					rel="noopener noreferrer"
					class="jv-dsub-btn jv-dsub-btn-primary"
					>Open sign-in URL
					<svg
						width="13"
						height="13"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
						<path d="M15 3h6v6" />
						<path d="M10 14 21 3" /></svg
				></a>
				<span v-else class="jv-dsub-muted">Starting sign-in…</span>
			</div>
			<div v-if="authorizeUrl" class="jv-dsub-urlrow">
				<code class="jv-dsub-url" :title="authorizeUrl">{{ authorizeUrl }}</code>
				<button type="button" class="jv-dsub-btn jv-dsub-btn-ghost" @click="copyUrl">
					{{ copied ? "Copied" : "Copy" }}
				</button>
			</div>
			<p v-if="codeOnly" class="jv-dsub-step" style="margin-top: 14px">
				<b>2.</b> After you click Authorize, {{ status.provider || pickProvider }} shows
				you an <b>authorization code</b>. Copy that code and paste it here:
			</p>
			<p v-else class="jv-dsub-step" style="margin-top: 14px">
				<b>2.</b> After you click Authorize, your browser shows a “This site can’t be
				reached” page. <b>That’s expected.</b> Copy the URL from the address bar (starts
				with <code>http://localhost:1455/auth/callback?code=…</code>) and paste it here:
			</p>
			<textarea
				v-model="pastedUrl"
				rows="3"
				class="jv-dsub-input"
				:placeholder="
					codeOnly
						? 'Paste the code shown after you approve'
						: 'Paste the URL from the error page here'
				"
			></textarea>
			<div class="jv-dsub-actions" style="margin-top: 10px">
				<button class="jv-dsub-btn jv-dsub-btn-ghost" @click="cancelFlow">Cancel</button>
				<button
					class="jv-dsub-btn jv-dsub-btn-primary"
					:disabled="busy"
					@click="submitPasted"
				>
					{{ busy ? "Connecting…" : "Submit" }}
				</button>
			</div>
			<div v-if="minsLeft !== null" class="jv-dsub-hint">
				Link valid for ~{{ minsLeft }} minute{{ minsLeft === 1 ? "" : "s" }}.
			</div>
			<div v-if="err" class="jv-dsub-err">{{ err }}</div>
		</div>

		<!-- ===== Connected (Screen 3) ===== -->
		<div v-else-if="status.connected">
			<p class="jv-dsub-muted" style="margin: 0 0 12px">
				Your chat subscription is served directly to the provider. Refresh state lives
				inside your Jarvis container - if chat starts failing, re-authorize to mint fresh
				tokens.
			</p>
			<div class="jv-dsub-kv">
				<span>Account</span><b>{{ status.account_email || "-" }}</b>
			</div>
			<div class="jv-dsub-kv">
				<span>Provider</span><b>{{ status.provider || "-" }}</b>
			</div>
			<div class="jv-dsub-kv">
				<span>Model</span><b>{{ status.model || "-" }}</b>
			</div>
			<div v-if="status.connected_at" class="jv-dsub-kv">
				<span>Connected</span><b>{{ connectedAtLabel }}</b>
			</div>
			<div class="jv-dsub-actions" style="margin-top: 14px">
				<button
					v-if="editable"
					class="jv-dsub-btn jv-dsub-btn-ghost"
					:disabled="busy"
					@click="doDisconnect"
				>
					Disconnect
				</button>
				<button
					v-if="editable"
					class="jv-dsub-btn jv-dsub-btn-primary"
					@click="startSignin()"
				>
					Re-authorize
				</button>
			</div>
			<div v-if="err" class="jv-dsub-err">{{ err }}</div>
		</div>

		<!-- ===== Not connected → connect a chat subscription (DIRECT) ===== -->
		<div v-else>
			<p
				v-if="status.is_single_subscription_pool"
				class="jv-dsub-muted"
				style="margin: 0 0 12px"
			>
				Your {{ status.provider || "chat subscription" }} is currently running through the
				<b>proxy</b>. Sign in below to switch it to <b>direct</b> - served straight to the
				provider (codex), lighter, with no proxy sidecar.
			</p>
			<p v-else class="jv-dsub-muted" style="margin: 0 0 12px">
				Sign in with your existing ChatGPT Plus/Pro or Gemini Advanced account - no API key
				needed. It's served directly to the provider (codex), not through the proxy.
			</p>
			<div class="jv-dsub-pick">
				<label class="jv-dsub-field">
					<span>Provider</span>
					<select
						v-model="pickProvider"
						class="jv-dsub-select"
						:disabled="!editable"
						@change="onPickProvider"
					>
						<option v-for="p in SUB_PROVIDERS" :key="p.provider" :value="p.provider">
							{{ p.provider }}
						</option>
					</select>
				</label>
				<label class="jv-dsub-field">
					<span>Default model</span>
					<select v-model="pickModel" class="jv-dsub-select" :disabled="!editable">
						<option v-for="m in pickModels" :key="m" :value="m">{{ m }}</option>
					</select>
				</label>
			</div>
			<div class="jv-dsub-actions" style="margin-top: 12px">
				<button
					v-if="editable"
					class="jv-dsub-btn jv-dsub-btn-primary"
					:disabled="busy"
					@click="startSignin(pickProvider, pickModel)"
				>
					Sign in with {{ pickProvider }}
				</button>
			</div>
			<div v-if="err" class="jv-dsub-err">{{ err }}</div>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import * as api from "@/api";
import { errMessage as _err } from "@/lib/errors";
import { isCodeOnlyPaste } from "@/llm/pool";
import { exactDate } from "@/utils/datetime";
import { useConfirm } from "@/composables/useConfirm";
import { agentName } from "@/branding";

const { confirm } = useConfirm();

const props = defineProps({
	// Shape from getDirectSubscriptionStatus(): { connected, provider, model,
	// account_email, connected_at, auth_mode, is_direct_subscription }.
	status: { type: Object, required: true },
	editable: { type: Boolean, default: true },
});
// reauthorized: a complete_paste_signin succeeded (parent reloads status +
// sync). disconnected: the subscription was torn down.
const emit = defineEmits(["reauthorized", "disconnected"]);

// Admin-managed model catalog (jarvis.chat.api.get_model_catalog_ui), fetched
// on mount. subscription_connect_providers is gated on a non-empty
// auth_profile_id (R7 - openai + google-gemini-cli today), never on
// supports_subscription, so it never offers a provider whose OAuth blob admin
// would reject. Falls back to the built-in SUB_PROVIDERS literal below when
// the fetch fails or hasn't landed yet - never blank.
const modelCatalog = ref({ subscription_connect_providers: [] });
onMounted(async () => {
	try {
		modelCatalog.value = (await api.getModelCatalogUi()) || modelCatalog.value;
	} catch (e) {
		/* built-in SUB_PROVIDERS fallback below covers this */
	}
});

// Built-in fallback: subscription providers offered for a fresh DIRECT connect
// before the catalog fetch lands or if it fails. Model lists mirror
// jarvis/_subscription_models.py (codex/gemini-cli catalog).
const FALLBACK_SUB_PROVIDERS = [
	{ provider: "OpenAI", models: ["gpt-5.5", "gpt-5.4"] },
	{ provider: "Google Gemini", models: ["gemini-2.5-pro", "gemini-2.5-flash"] },
];
// Gated server-side on a non-empty auth_profile_id (R7): supports_subscription
// is true for xai and moonshot too (cliproxy really does serve their
// subscription models), but only openai/google-gemini-cli support this card's
// paste-back connect flow - Kimi is device-code and admin's push_oauth_blob
// rejects a direct xAI blob outright. Data now, not a literal; the rendered
// list still stays two entries because the seed only sets auth_profile_id for
// those two.
const SUB_PROVIDERS = computed(() => {
	const rows = modelCatalog.value.subscription_connect_providers || [];
	return rows.length ? rows : FALLBACK_SUB_PROVIDERS;
});
function _defaultProvider() {
	if (SUB_PROVIDERS.value.some((p) => p.provider === props.status.provider))
		return props.status.provider;
	const byModel = SUB_PROVIDERS.value.find((p) => p.models.includes(props.status.model));
	return byModel ? byModel.provider : "OpenAI";
}
const pickProvider = ref(_defaultProvider());
// The Re-authorize path reuses `status.provider` regardless of SUB_PROVIDERS
// (which only gates NEW connections), so a tenant already stored as "xAI Grok"
// can reach this flow. xAI shows a bare code, not a callback URL, so the step-2
// copy has to follow. Shared with LlmPoolEditor via @/llm/pool.
// props.status, NOT status.value: there is no local `status` ref in this file, so the
// bare name resolved to the browser global `window.status` (a string). `.value` on it
// is undefined, so this computed threw every time it evaluated - i.e. whenever the
// paste-back screen rendered, which is the entire xAI bare-code flow. Every other
// reference here already uses props.status.
const codeOnly = computed(() => isCodeOnlyPaste(props.status.provider || pickProvider.value));
const pickModels = computed(
	() =>
		(
			SUB_PROVIDERS.value.find((p) => p.provider === pickProvider.value) ||
			SUB_PROVIDERS.value[0]
		).models
);
const pickModel = ref(
	pickModels.value.includes(props.status.model) ? props.status.model : pickModels.value[0]
);
function onPickProvider() {
	pickModel.value = pickModels.value[0];
}

const flowOpen = ref(false);
const nonce = ref("");
const authorizeUrl = ref("");
const expiresAt = ref(0);
const pastedUrl = ref("");
const busy = ref(false);
const err = ref("");
const copied = ref(false);

// Reactive clock so the "Link valid for ~N minutes" hint actually counts down.
// Date.now() alone is non-reactive, so a computed over it would freeze at first
// paint; nowTick is bumped by an interval that only runs while a link is live.
const nowTick = ref(Date.now());
let ticker = null;
function startTicker() {
	stopTicker();
	ticker = setInterval(() => {
		nowTick.value = Date.now();
	}, 30000);
}
function stopTicker() {
	if (ticker) {
		clearInterval(ticker);
		ticker = null;
	}
}
onBeforeUnmount(stopTicker);

const minsLeft = computed(() =>
	expiresAt.value ? Math.max(0, Math.floor((expiresAt.value - nowTick.value) / 60000)) : null
);
// exactDate parses the server's naive datetime in the SITE timezone (via
// frappe-ui dayjsLocal) and renders it in the viewer's zone - do NOT hand the
// raw string to new Date(), which reads it as browser-local (wrong offset) and
// can yield Invalid Date on strict engines for the 6-digit microsecond suffix.
const connectedAtLabel = computed(() => exactDate(props.status.connected_at) || "-");

function resetFlow() {
	flowOpen.value = false;
	nonce.value = "";
	authorizeUrl.value = "";
	expiresAt.value = 0;
	pastedUrl.value = "";
	copied.value = false;
	stopTicker();
}
function cancelFlow() {
	resetFlow();
	err.value = "";
}

// Start the DIRECT paste-back sign-in. Re-authorize (no args) reuses the
// connected provider/model; a fresh connect passes the picked provider/model.
// The backend coerces the model to a valid subscription model for the provider,
// and complete_paste_signin clears any existing models[] pool + sets the direct
// flat fields - so this doubles as the "switch a pooled subscription to direct".
async function startSignin(providerOverride, modelOverride) {
	err.value = "";
	busy.value = true;
	flowOpen.value = true;
	authorizeUrl.value = "";
	try {
		const p = (providerOverride || props.status.provider || pickProvider.value || "").trim();
		const m = (modelOverride || props.status.model || pickModel.value || "").trim();
		const res = await api.beginPasteSignin(p, m);
		if (!res || res.ok === false) {
			err.value =
				(res && res.error && res.error.message) || "Couldn't start sign-in - try again.";
			flowOpen.value = false;
			return;
		}
		const d = res.data || {};
		nonce.value = d.nonce;
		authorizeUrl.value = d.authorize_url;
		expiresAt.value = Date.now() + (Number(d.expires_in) || 0) * 1000;
		startTicker();
	} catch (e) {
		err.value = _err(e);
		flowOpen.value = false;
	} finally {
		busy.value = false;
	}
}

async function submitPasted() {
	err.value = "";
	const pasted = (pastedUrl.value || "").trim();
	if (!pasted) {
		err.value = codeOnly.value
			? "Paste the code you were shown first."
			: "Paste the URL from your browser's address bar first.";
		return;
	}
	busy.value = true;
	try {
		const res = await api.completePasteSignin(nonce.value, pasted);
		if (!res || res.ok === false) {
			const code = (res && res.error && res.error.code) || "";
			err.value = `${code ? code + ": " : ""}${
				(res && res.error && res.error.message) || "Sign-in failed."
			}`;
			// A dead nonce can't be retried - drop back to the connected screen.
			if (code === "expired" || code === "unknown_nonce") resetFlow();
			return;
		}
		resetFlow();
		emit("reauthorized", res.data || {});
	} catch (e) {
		err.value = _err(e);
	} finally {
		busy.value = false;
	}
}

async function doDisconnect() {
	if (
		!(await confirm({
			title: "Disconnect chat subscription?",
			message: `${agentName} chat will stop working until you reconnect.`,
			confirmLabel: "Disconnect",
			danger: true,
		}))
	)
		return;
	err.value = "";
	busy.value = true;
	try {
		const res = await api.disconnectSubscription();
		if (!res || res.ok === false) {
			err.value = (res && res.error && res.error.message) || "Disconnect failed.";
			return;
		}
		resetFlow();
		emit("disconnected");
	} catch (e) {
		err.value = _err(e);
	} finally {
		busy.value = false;
	}
}

function copyUrl() {
	const url = authorizeUrl.value;
	if (!url) return;
	const done = () => {
		copied.value = true;
		setTimeout(() => {
			copied.value = false;
		}, 1400);
	};
	const fail = () => {
		err.value = "Could not copy - select the URL and copy manually.";
	};
	if (navigator.clipboard && window.isSecureContext) {
		navigator.clipboard.writeText(url).then(done).catch(fail);
		return;
	}
	// LAN HTTP fallback: navigator.clipboard is undefined in insecure contexts.
	// execCommand can return false WITHOUT throwing (e.g. selection disallowed) -
	// honour the boolean so a failed copy doesn't falsely flash "Copied ✓".
	const ta = document.createElement("textarea");
	ta.value = url;
	ta.style.position = "fixed";
	ta.style.left = "-9999px";
	document.body.appendChild(ta);
	ta.focus();
	ta.select();
	let ok = false;
	try {
		ok = document.execCommand("copy");
	} catch (e) {
		ok = false;
	}
	document.body.removeChild(ta);
	ok ? done() : fail();
}
</script>

<style scoped>
.jv-dsub-step {
	font-size: 13px;
	color: var(--text-2);
	margin: 0 0 8px;
	line-height: 1.5;
}
.jv-dsub-muted {
	font-size: 13px;
	color: var(--text-3);
}
.jv-dsub-actions {
	display: flex;
	gap: 10px;
	flex-wrap: wrap;
	align-items: center;
}
/* Sized to match the jv-btn jv-btn--sm compact convention used elsewhere in
   the settings panes (e.g. AiModelsPane's own Retry button, GeneralPane's
   Delete-all) — Reconnect/Disconnect were previously oversized (13px/8x16). */
.jv-dsub-btn {
	font-size: 12px;
	font-weight: 600;
	height: 32px;
	padding: 0 12px;
	border-radius: 9px;
	cursor: pointer;
	border: 1px solid var(--border);
	text-decoration: none;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	line-height: 1;
}
.jv-dsub-btn:disabled {
	opacity: 0.5;
	cursor: not-allowed;
}
.jv-dsub-btn-primary {
	background: var(--text);
	color: var(--surface);
	border-color: var(--text);
	gap: 6px;
	font-family: inherit;
	transition: background-color 0.15s ease, border-color 0.15s ease;
}
.jv-dsub-btn-primary:hover {
	background: var(--text-2);
	border-color: var(--text-2);
}
.jv-dsub-btn-primary svg {
	stroke: var(--surface);
}
.jv-dsub-btn-ghost {
	background: var(--surface-2);
	color: var(--text);
	border-color: transparent;
	font-family: inherit;
	transition: background-color 0.15s ease;
}
.jv-dsub-btn-ghost:hover {
	background: var(--surface-3);
}
.jv-dsub-input {
	width: 100%;
	box-sizing: border-box;
	padding: 9px 12px;
	font-size: 14px;
	border: 1px solid var(--border-2);
	border-radius: 8px;
	background: var(--surface);
	color: var(--text);
	font-family: inherit;
	resize: vertical;
}
.jv-dsub-urlrow {
	display: flex;
	gap: 8px;
	align-items: center;
}
.jv-dsub-url {
	flex: 1;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 12px;
	padding: 7px 10px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface-2);
	color: var(--text-2);
}
/* Match AccountView's sibling .jv-acct-kv rows so the two cards read as one system. */
.jv-dsub-kv {
	display: flex;
	justify-content: space-between;
	gap: 12px;
	font-size: 13px;
	padding: 5px 0;
	border-bottom: 1px solid var(--border);
}
.jv-dsub-kv span {
	color: var(--text-3);
}
.jv-dsub-kv b {
	text-align: right;
	word-break: break-word;
}
.jv-dsub-hint {
	margin-top: 12px;
	font-size: 12px;
	color: var(--text-3);
}
.jv-dsub-err {
	margin-top: 10px;
	font-size: 13px;
	color: var(--red);
}
.jv-dsub-pick {
	display: flex;
	gap: 12px;
	flex-wrap: wrap;
}
.jv-dsub-field {
	display: flex;
	flex-direction: column;
	gap: 4px;
	flex: 1 1 200px;
	min-width: 0;
}
.jv-dsub-field span {
	font-size: 12px;
	color: var(--text-3);
}
.jv-dsub-select {
	padding: 7px 12px;
	font-size: 13px;
	border: 1px solid var(--border-2);
	border-radius: 8px;
	background: var(--surface);
	color: var(--text);
	font-family: inherit;
}
</style>
