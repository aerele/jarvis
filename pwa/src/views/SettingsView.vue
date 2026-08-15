<script setup>
import { computed, onMounted, ref } from "vue";
import AppBar from "../components/AppBar.vue";
import Sheet from "../components/Sheet.vue";
import * as api from "../api";
import { EFFORT, prefs, setPrefs } from "../lib/prefs";
import { applyTheme, theme } from "../lib/theme";
import { agentName } from "@/branding";

// Settings, as the native app has them: Chat (default model + effort),
// Appearance, Notifications. No "open full workspace" — this is the workspace.
const settings = ref(null);
const modelSheet = ref(false);
const notifyError = ref("");

const THEMES = [
	{ value: "light", label: "Light" },
	{ value: "dark", label: "Dark" },
	{ value: "system", label: "System" },
];

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

function pick(m) {
	// Tapping the current one clears the override — back to the workspace default.
	setPrefs({ defaultModel: m === prefs.defaultModel ? "" : m });
	modelSheet.value = false;
}

// A toggle that promises a notification has to be able to deliver one. Ask for
// permission at the moment the user opts in — asking on load is the behaviour
// every browser now punishes, and a toggle that silently does nothing is worse
// than no toggle.
async function toggleNotify(key) {
	notifyError.value = "";
	const turningOn = !prefs[key];
	if (turningOn && "Notification" in window && Notification.permission !== "granted") {
		const res = await Notification.requestPermission();
		if (res !== "granted") {
			notifyError.value =
				res === "denied"
					? "Notifications are blocked for this site in your browser settings."
					: "Notifications need your permission.";
			return;
		}
	}
	setPrefs({ [key]: turningOn });
}

const notifySupported = "Notification" in window;

onMounted(async () => {
	try {
		settings.value = await api.getChatUiSettings();
	} catch {
		/* the rest of the screen still works */
	}
});
</script>

<template>
	<AppBar title="Settings" />

	<div class="jv-scroll jv-pad">
		<div class="jv-label">Chat</div>
		<div class="jv-card">
			<button class="jv-row is-tap" @click="modelSheet = true">
				<span>Default model</span>
				<span class="jv-row-right">
					<strong>{{ currentModel || "Workspace default" }}</strong>
					<svg
						viewBox="0 0 24 24"
						width="15"
						height="15"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="m9 18 6-6-6-6" />
					</svg>
				</span>
			</button>
			<div class="jv-block">
				<div class="jv-block-title">Thinking level</div>
				<div class="jv-seg">
					<button
						v-for="e in EFFORT"
						:key="e.value"
						class="jv-seg-btn"
						:class="{ 'is-on': prefs.effort === e.value }"
						@click="setPrefs({ effort: e.value })"
					>
						{{ e.value }}
					</button>
				</div>
			</div>
		</div>

		<div class="jv-label">Appearance</div>
		<div class="jv-seg is-loose">
			<button
				v-for="t in THEMES"
				:key="t.value"
				class="jv-seg-btn"
				:class="{ 'is-on': theme === t.value }"
				@click="applyTheme(t.value)"
			>
				{{ t.label }}
			</button>
		</div>

		<template v-if="notifySupported">
			<div class="jv-label">Notifications</div>
			<div class="jv-card">
				<div class="jv-row">
					<span>Task finished</span>
					<button
						class="jv-toggle"
						:class="{ 'is-on': prefs.notifyDone }"
						role="switch"
						:aria-checked="prefs.notifyDone"
						@click="toggleNotify('notifyDone')"
					>
						<span />
					</button>
				</div>
				<div class="jv-row is-last">
					<span>Needs your decision</span>
					<button
						class="jv-toggle"
						:class="{ 'is-on': prefs.notifyDecision }"
						role="switch"
						:aria-checked="prefs.notifyDecision"
						@click="toggleNotify('notifyDecision')"
					>
						<span />
					</button>
				</div>
			</div>
			<div v-if="notifyError" class="jv-err">{{ notifyError }}</div>
			<p class="jv-hint">
				{{ agentName }} only buzzes when this tab isn't the one you're looking at.
			</p>
		</template>
	</div>

	<Sheet :open="modelSheet" @close="modelSheet = false">
		<div class="jv-msheet">
			<div class="jv-msheet-title">Default model</div>
			<div v-if="!models.length" class="jv-mnone">
				No models published for your workspace yet.
			</div>
			<button v-for="m in models" :key="m" class="jv-mrow" @click="pick(m)">
				<span>{{ m }}</span>
				<svg
					v-if="m === currentModel"
					viewBox="0 0 24 24"
					width="17"
					height="17"
					fill="none"
					stroke="currentColor"
					stroke-width="2.4"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M20 6 9 17l-5-5" />
				</svg>
			</button>
		</div>
	</Sheet>
</template>

<style scoped>
.jv-pad {
	padding: 8px 16px 40px;
}
.jv-label {
	margin: 12px 2px 8px;
	font-size: 11px;
	font-weight: 600;
	letter-spacing: 0.6px;
	text-transform: uppercase;
	color: var(--ink5);
}
.jv-card {
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-row {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	width: 100%;
	padding: 13px;
	border: 0;
	border-bottom: 1px solid var(--border);
	background: transparent;
	color: var(--ink8);
	font: inherit;
	font-size: 13.5px;
	text-align: left;
}
.jv-row.is-last {
	border-bottom: 0;
}
.jv-row.is-tap {
	cursor: pointer;
}
.jv-row.is-tap:active {
	background: var(--card2);
}
.jv-row-right {
	display: flex;
	align-items: center;
	gap: 5px;
	min-width: 0;
	color: var(--ink6);
}
.jv-row-right strong {
	font-size: 13px;
	font-weight: 500;
	color: var(--ink6);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-block {
	padding: 13px;
}
.jv-block-title {
	margin-bottom: 9px;
	font-size: 13.5px;
	color: var(--ink8);
}
.jv-seg {
	display: flex;
	gap: 4px;
	padding: 4px;
	border-radius: 10px;
	background: var(--card2);
}
.jv-seg.is-loose {
	border: 1px solid var(--border);
	background: var(--card);
}
.jv-seg-btn {
	flex: 1;
	height: 36px;
	border: 0;
	border-radius: 8px;
	background: transparent;
	color: var(--ink6);
	font: inherit;
	font-size: 13.5px;
	font-weight: 500;
	cursor: pointer;
}
.jv-seg-btn.is-on {
	background: var(--card);
	color: var(--accent);
	font-weight: 600;
	box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.jv-seg.is-loose .jv-seg-btn.is-on {
	background: var(--accent-bg);
}
.jv-toggle {
	flex: none;
	width: 44px;
	height: 26px;
	padding: 3px;
	border: 0;
	border-radius: 999px;
	background: var(--card3);
	cursor: pointer;
	transition: background 0.15s ease;
}
.jv-toggle span {
	display: block;
	width: 20px;
	height: 20px;
	border-radius: 999px;
	background: #fff;
	transition: transform 0.15s ease;
}
.jv-toggle.is-on {
	background: var(--accent-solid);
}
.jv-toggle.is-on span {
	transform: translateX(18px);
}
.jv-err {
	margin-top: 8px;
	font-size: 12.5px;
	color: var(--red);
}
.jv-hint {
	margin: 8px 2px 0;
	font-size: 12px;
	line-height: 1.45;
	color: var(--ink5);
}

.jv-msheet {
	padding: 8px 16px 24px;
}
.jv-msheet-title {
	margin: 0 2px 6px;
	font-size: 15px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-mrow {
	display: flex;
	align-items: center;
	justify-content: space-between;
	width: 100%;
	padding: 13px 2px;
	border: 0;
	border-bottom: 1px solid var(--border);
	background: transparent;
	color: var(--ink8);
	font: inherit;
	font-size: 14px;
	text-align: left;
	cursor: pointer;
}
.jv-mrow:last-child {
	border-bottom: 0;
}
.jv-mrow svg {
	color: var(--accent);
}
.jv-mnone {
	padding: 13px 2px;
	font-size: 12.5px;
	color: var(--ink5);
}
</style>
