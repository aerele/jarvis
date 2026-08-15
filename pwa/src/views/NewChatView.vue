<script setup>
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from "vue";
import BrandMark from "../components/BrandMark.vue";
import { agentName } from "@/branding";
import { useRouter } from "vue-router";
import * as api from "../api";
import { store } from "../store";
import { EFFORT, prefs, setPrefs, thinkingOf } from "../lib/prefs";
import { feed } from "../lib/notifications";
import Sheet from "../components/Sheet.vue";

// New chat: the hero screen, not an empty thread with a chat bar bolted to the
// bottom. Brand mark, a greeting that knows the time of day and who you are, and
// one card that holds the whole composer — attachments, model, mic, send.
const VoiceSheet = defineAsyncComponent(() => import("../components/VoiceSheet.vue"));

const router = useRouter();

const input = ref("");
const busy = ref(false);
const error = ref("");
const attachments = ref([]);
const settings = ref(null);
const modelSheet = ref(false);
const voiceOpen = ref(false);
const inputEl = ref(null);
const fileEl = ref(null);

// Morning / afternoon / evening, by the clock on THIS device — the phone's own
// timezone is the one the user is standing in.
const greeting = computed(() => {
	const h = new Date().getHours();
	const part = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
	const first = String(window.frappe_full_name || "")
		.trim()
		.split(/\s+/)[0];
	return first ? `${part}, ${first}.` : `${part}.`;
});

// Same derivation the desktop uses: the configured pool if there is one, else
// the provider's subscription allowlist. Deduped — a pool holds one row per
// account, not per model.
const models = computed(() => {
	const s = settings.value;
	if (!s) return [];
	const pool = s.pool_models || [];
	if (pool.length) {
		const seen = new Set();
		const out = [];
		for (const r of pool) {
			if (!r.model || seen.has(r.model)) continue;
			seen.add(r.model);
			out.push(r.model);
		}
		return out;
	}
	return s.subscription_models?.[s.llm_provider] || [];
});

const currentModel = computed(() => prefs.defaultModel || settings.value?.llm_model || "");
const micEnabled = computed(() => !!settings.value?.stt_enabled);
const hasDraft = computed(
	() => input.value.trim().length > 0 || attachments.value.some((a) => a.file_url)
);
const uploading = computed(() => attachments.value.some((a) => a.uploading));

function modelDesc(name) {
	const n = String(name).toLowerCase();
	if (n.includes("opus") || n.includes("5.5"))
		return "Most capable, for complex multi-step work";
	if (n.includes("sonnet")) return "Balanced speed and intelligence";
	if (n.includes("haiku") || n.includes("mini") || n.includes("flash"))
		return "Fastest, for quick everyday tasks";
	return "Available on your plan";
}

function autoGrow() {
	const el = inputEl.value;
	if (!el) return;
	el.style.height = "auto";
	el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
}

async function send(text = input.value) {
	const t = String(text).trim();
	const ready = attachments.value.filter((a) => a.file_url);
	if ((!t && !ready.length) || busy.value || uploading.value) return;

	busy.value = true;
	error.value = "";
	try {
		// conversation "" → the backend creates (or focuses) the empty one and
		// hands back its id, so a new chat costs one round-trip, not two.
		const r = await api.sendMessage("", t, {
			attachments: ready.map((a) => ({ file_url: a.file_url, file_name: a.name })),
			model: prefs.defaultModel || "",
			thinking: thinkingOf(prefs.effort),
		});
		if (r?.ok === false || !r?.conversation_id) {
			error.value = r?.reason || "Couldn't start that chat.";
			busy.value = false;
			return;
		}
		input.value = "";
		attachments.value = [];
		store.loadConversations();
		router.push(`/c/${r.conversation_id}`);
	} catch (e) {
		error.value = e?.message || "Couldn't start that chat.";
		busy.value = false;
	}
}

async function attach(e) {
	const files = [...(e.target.files || [])];
	e.target.value = "";
	if (!files.length) return;
	error.value = "";

	const staged = files.map((f, i) => ({
		key: `att-${Date.now()}-${i}`,
		name: f.name,
		file: f,
		preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : "",
		uploading: true,
	}));
	attachments.value.push(...staged);

	await Promise.all(
		staged.map(async (a) => {
			try {
				const up = await api.uploadFile(a.file);
				const row = attachments.value.find((x) => x.key === a.key);
				if (row) {
					row.file_url = up.file_url;
					row.uploading = false;
				}
			} catch (err) {
				removeAttachment(a.key);
				error.value = err?.message || `Couldn't upload ${a.name}.`;
			}
		})
	);
}

function removeAttachment(key) {
	const row = attachments.value.find((a) => a.key === key);
	if (row?.preview) URL.revokeObjectURL(row.preview);
	attachments.value = attachments.value.filter((a) => a.key !== key);
}

function onKeydown(e) {
	if (e.key === "Enter" && !e.shiftKey && !/Mobi|Android/i.test(navigator.userAgent)) {
		e.preventDefault();
		send();
	}
}

onMounted(async () => {
	try {
		settings.value = await api.getChatUiSettings();
	} catch {
		/* the screen still works without the model chip */
	}
});
onUnmounted(() => attachments.value.forEach((a) => a.preview && URL.revokeObjectURL(a.preview)));
</script>

<template>
	<div class="jv-bar is-bare">
		<button class="jv-icon-btn" aria-label="Menu" @click="store.drawerOpen = true">
			<svg
				viewBox="0 0 24 24"
				width="20"
				height="20"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
			>
				<path d="M3 6h18M3 12h18M3 18h18" />
			</svg>
		</button>
		<span class="jv-spacer" />
		<!-- Same place as the native app: the bell lives on the new-chat header
		     only. It is where you land when you come back to the app, which is
		     exactly when you want to know what happened while you were gone. -->
		<button
			class="jv-icon-btn"
			aria-label="Notifications"
			@click="router.push('/notifications')"
		>
			<svg
				viewBox="0 0 24 24"
				width="20"
				height="20"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0" />
			</svg>
			<span v-if="feed.unread" class="jv-bell-dot" />
		</button>
	</div>

	<div class="jv-hero">
		<BrandMark :size="56" />
		<h1 class="jv-greeting">{{ greeting }}</h1>
	</div>

	<div class="jv-heroc jv-safe-bottom">
		<div v-if="error" class="jv-heroc-err">
			<svg
				viewBox="0 0 24 24"
				width="14"
				height="14"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
			>
				<circle cx="12" cy="12" r="9" />
				<path d="M12 8v5M12 16h.01" />
			</svg>
			{{ error }}
		</div>

		<!-- One card: attachments, the field, and every control that acts on it.
		     The chat bar belongs in a chat; a blank screen deserves a composer. -->
		<div class="jv-card">
			<div v-if="attachments.length" class="jv-atts">
				<div v-for="a in attachments" :key="a.key" class="jv-att">
					<img v-if="a.preview" class="jv-att-img" :src="a.preview" :alt="a.name" />
					<div v-else class="jv-att-file">
						<svg
							viewBox="0 0 24 24"
							width="16"
							height="16"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
							<path d="M14 2v6h6" />
						</svg>
						<span class="jv-att-name">{{ a.name }}</span>
					</div>
					<div v-if="a.uploading" class="jv-att-busy">
						<span class="jv-spinner is-light" />
					</div>
					<button class="jv-att-x" aria-label="Remove" @click="removeAttachment(a.key)">
						<svg
							viewBox="0 0 24 24"
							width="11"
							height="11"
							fill="none"
							stroke="currentColor"
							stroke-width="2.6"
							stroke-linecap="round"
						>
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
					</button>
				</div>
			</div>

			<textarea
				ref="inputEl"
				v-model="input"
				rows="1"
				:placeholder="`Message ${agentName}…`"
				@input="autoGrow"
				@keydown="onKeydown"
			/>

			<div class="jv-card-row">
				<input ref="fileEl" type="file" multiple hidden @change="attach" />
				<button class="jv-round" aria-label="Attach a file" @click="fileEl.click()">
					<svg
						viewBox="0 0 24 24"
						width="17"
						height="17"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
					>
						<path d="M12 5v14M5 12h14" />
					</svg>
				</button>

				<button v-if="models.length" class="jv-modelchip" @click="modelSheet = true">
					<span class="jv-dot" />
					<span class="jv-modelchip-name">{{ currentModel || "Model" }}</span>
					<svg
						viewBox="0 0 24 24"
						width="13"
						height="13"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="m6 9 6 6 6-6" />
					</svg>
				</button>

				<span class="jv-spacer" />

				<button
					v-if="!hasDraft && micEnabled"
					class="jv-mic"
					aria-label="Dictate"
					@click="voiceOpen = true"
				>
					<svg
						viewBox="0 0 24 24"
						width="19"
						height="19"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
						<path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" />
					</svg>
				</button>
				<button
					v-if="hasDraft"
					class="jv-send"
					aria-label="Send"
					:disabled="busy || uploading"
					@click="send()"
				>
					<span v-if="busy" class="jv-spinner is-light" />
					<svg
						v-else
						viewBox="0 0 24 24"
						width="19"
						height="19"
						fill="none"
						stroke="currentColor"
						stroke-width="2.1"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M12 19V5M5 12l7-7 7 7" />
					</svg>
				</button>
			</div>
		</div>
	</div>

	<!-- model + effort -->
	<Sheet :open="modelSheet" @close="modelSheet = false">
		<div class="jv-msheet">
			<div class="jv-msheet-head">
				<span>Model</span>
				<button class="jv-x" aria-label="Close" @click="modelSheet = false">
					<svg
						viewBox="0 0 24 24"
						width="16"
						height="16"
						fill="none"
						stroke="currentColor"
						stroke-width="2.2"
						stroke-linecap="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="jv-msheet-body">
				<button
					v-for="m in models"
					:key="m"
					class="jv-mrow"
					@click="setPrefs({ defaultModel: m === currentModel ? '' : m })"
				>
					<span class="jv-radio" :class="{ 'is-on': m === currentModel }" />
					<span class="jv-mrow-text">
						<span class="jv-mrow-name">{{ m }}</span>
						<span class="jv-mrow-desc">{{ modelDesc(m) }}</span>
					</span>
					<svg
						v-if="m === currentModel"
						class="jv-check"
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						stroke-width="2.4"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M20 6 9 17l-5-5" />
					</svg>
				</button>
				<div v-if="!models.length" class="jv-mnone">
					No models available on your plan yet.
				</div>

				<div class="jv-sep" />

				<div class="jv-msheet-sub">Effort</div>
				<div class="jv-msheet-hint">How much {{ agentName }} thinks before it acts.</div>
				<div class="jv-seg">
					<button
						v-for="e in EFFORT"
						:key="e.value"
						class="jv-seg-btn"
						:class="{ 'is-on': prefs.effort === e.value }"
						@click="setPrefs({ effort: e.value })"
					>
						<span>{{ e.value }}</span>
						<small>{{ e.hint }}</small>
					</button>
				</div>

				<button class="jv-done" @click="modelSheet = false">Done</button>
			</div>
		</div>
	</Sheet>

	<VoiceSheet :open="voiceOpen" @close="voiceOpen = false" @transcript="(t) => send(t)" />
</template>

<style scoped>
.jv-bar.is-bare {
	background: transparent;
	border-bottom: 0;
}
.jv-icon-btn {
	position: relative;
}
/* Unread work is waiting. A count would be false precision — the feed is
   assembled from live events, so "some" is the only honest number. */
.jv-bell-dot {
	position: absolute;
	top: 8px;
	right: 8px;
	width: 8px;
	height: 8px;
	border-radius: 999px;
	background: var(--accent);
	border: 2px solid var(--bg);
}
.jv-hero {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	gap: 14px;
	padding: 18px;
}
/* The name is arbitrary-length, so this line has to survive "Administrator"
   as well as "Jo". At 24px/600 the longest common case measured 326px against
   324px of usable width, so it overflowed its own padding and sat edge to edge.
   Smaller and lighter, capped short of the padding, and balanced so a wrap
   splits evenly instead of dropping one orphaned word. */
.jv-greeting {
	margin: 0;
	max-width: 20ch;
	font-size: 21px;
	font-weight: 550;
	letter-spacing: -0.3px;
	line-height: 1.25;
	color: var(--ink9);
	text-align: center;
	text-wrap: balance;
}

.jv-heroc {
	flex: none;
	padding: 6px 14px 10px;
}
.jv-heroc-err {
	display: flex;
	align-items: center;
	gap: 8px;
	margin-bottom: 8px;
	padding: 10px;
	border-radius: 10px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 12px;
	font-weight: 500;
}
.jv-card {
	padding: 14px 15px 11px;
	border: 1px solid var(--border2);
	border-radius: 20px;
	background: var(--card);
	box-shadow: 0 1px 2px rgba(0, 0, 0, 0.07);
}
.jv-card textarea {
	display: block;
	width: 100%;
	border: 0;
	outline: none;
	resize: none;
	background: transparent;
	color: var(--ink9);
	font: inherit;
	/* 15.5px, not 14.5: this is the one field the user actually composes in,
	   and the reference apps sit around 16px. Below that it reads as a caption
	   rather than the primary input. */
	font-size: 15.5px;
	line-height: 1.45;
	padding: 2px 0 12px;
	max-height: 120px;
}
.jv-card-row {
	display: flex;
	align-items: center;
	gap: 8px;
}
.jv-spacer {
	flex: 1;
}
.jv-round {
	display: grid;
	place-items: center;
	width: 34px;
	height: 34px;
	flex: none;
	border: 1px solid var(--border2);
	border-radius: 999px;
	background: transparent;
	color: var(--ink6);
	cursor: pointer;
}
.jv-modelchip {
	display: flex;
	align-items: center;
	gap: 6px;
	max-width: 190px;
	height: 34px;
	padding: 0 12px;
	border: 1px solid var(--border2);
	border-radius: 999px;
	background: transparent;
	color: var(--ink7);
	font: inherit;
	cursor: pointer;
}
.jv-modelchip-name {
	font-size: 12.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-dot {
	width: 6px;
	height: 6px;
	flex: none;
	border-radius: 999px;
	background: var(--accent);
}
.jv-mic {
	display: grid;
	place-items: center;
	width: 38px;
	height: 38px;
	flex: none;
	border: 0;
	border-radius: 999px;
	background: var(--card3);
	color: var(--ink8);
	cursor: pointer;
}
.jv-send {
	display: grid;
	place-items: center;
	width: 38px;
	height: 38px;
	flex: none;
	border: 0;
	border-radius: 999px;
	background: var(--inv-bg);
	color: var(--inv-ink);
	cursor: pointer;
}
.jv-send:disabled {
	opacity: 0.5;
}

/* attachments */
.jv-atts {
	display: flex;
	flex-wrap: wrap;
	gap: 12px;
	padding-bottom: 12px;
}
.jv-att {
	position: relative;
}
.jv-att-img {
	display: block;
	width: 52px;
	height: 52px;
	object-fit: cover;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--card2);
}
.jv-att-file {
	display: flex;
	align-items: center;
	gap: 7px;
	max-width: 180px;
	height: 52px;
	padding: 0 10px;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--card2);
	color: var(--ink6);
}
.jv-att-name {
	font-size: 11.5px;
	font-weight: 500;
	color: var(--ink8);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-att-busy {
	position: absolute;
	inset: 0;
	display: grid;
	place-items: center;
	border-radius: 9px;
	background: rgba(0, 0, 0, 0.35);
}
.jv-att-x {
	position: absolute;
	top: -6px;
	right: -6px;
	display: grid;
	place-items: center;
	width: 20px;
	height: 20px;
	padding: 0;
	border: 2px solid var(--card);
	border-radius: 999px;
	background: var(--inv-bg);
	color: var(--inv-ink);
	cursor: pointer;
}

/* model sheet */
.jv-msheet {
	display: flex;
	flex-direction: column;
	min-height: 0;
}
.jv-msheet-head {
	display: flex;
	align-items: center;
	padding: 6px 20px 4px;
	font-size: 16px;
	font-weight: 600;
	letter-spacing: -0.2px;
	color: var(--ink9);
}
.jv-msheet-head span {
	flex: 1;
}
.jv-x {
	display: grid;
	place-items: center;
	width: 30px;
	height: 30px;
	border: 0;
	border-radius: 999px;
	background: var(--card2);
	color: var(--ink6);
	cursor: pointer;
}
.jv-msheet-body {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 6px 12px 18px;
}
.jv-mrow {
	display: flex;
	align-items: flex-start;
	gap: 11px;
	width: 100%;
	padding: 11px 12px;
	border: 0;
	border-radius: 12px;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-mrow:active {
	background: var(--card2);
}
.jv-radio {
	width: 9px;
	height: 9px;
	margin-top: 5px;
	flex: none;
	border-radius: 999px;
	background: var(--ink3);
}
.jv-radio.is-on {
	background: var(--accent);
}
.jv-mrow-text {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-mrow-name {
	font-size: 14px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-mrow-desc {
	margin-top: 2px;
	font-size: 12px;
	line-height: 1.4;
	color: var(--ink5);
}
.jv-check {
	flex: none;
	margin-top: 2px;
	color: var(--accent);
}
.jv-mnone {
	padding: 12px;
	font-size: 12.5px;
	color: var(--ink5);
}
.jv-sep {
	height: 1px;
	margin: 8px 8px;
	background: var(--border);
}
.jv-msheet-sub {
	padding: 0 8px;
	font-size: 14px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-msheet-hint {
	padding: 4px 8px 8px;
	font-size: 12px;
	line-height: 1.4;
	color: var(--ink5);
}
.jv-seg {
	display: flex;
	gap: 4px;
	padding: 4px;
	margin: 0 8px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card2);
}
.jv-seg-btn {
	flex: 1;
	display: flex;
	flex-direction: column;
	gap: 1px;
	padding: 8px 4px;
	border: 0;
	border-radius: 9px;
	background: transparent;
	color: var(--ink6);
	font: inherit;
	cursor: pointer;
}
.jv-seg-btn span {
	font-size: 13.5px;
	font-weight: 500;
}
.jv-seg-btn small {
	font-size: 10.5px;
	color: var(--ink4);
}
.jv-seg-btn.is-on {
	background: var(--card);
	color: var(--accent);
	box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.jv-seg-btn.is-on span {
	font-weight: 600;
}
.jv-done {
	width: calc(100% - 16px);
	height: 46px;
	margin: 16px 8px 0;
	border: 0;
	border-radius: 12px;
	background: var(--accent-solid);
	color: #fff;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-spinner {
	width: 16px;
	height: 16px;
	border-radius: 50%;
	border: 2px solid var(--card3);
	border-top-color: var(--accent);
	animation: jv-spin 0.7s linear infinite;
}
.jv-spinner.is-light {
	border-color: rgba(255, 255, 255, 0.35);
	border-top-color: #fff;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
