<script setup>
import { computed, defineAsyncComponent, onMounted, ref } from "vue";
import AppBar from "../components/AppBar.vue";
import * as api from "../api";
import { relativeTime } from "../lib/time";
import { agentName } from "@/branding";

// Business: what the agent knows about how YOUR company works. Notes captured
// here are processed daily into learned defaults and the org wiki, so the agent
// stops asking the same question twice. The web calls this Personalise; the
// endpoints (voice_notes_api) are the same ones.
const VoiceSheet = defineAsyncComponent(() => import("../components/VoiceSheet.vue"));

const notes = ref([]);
const status = ref(null);
const loaded = ref(false);
const draft = ref("");
const saving = ref(false);
const error = ref("");
const voiceOpen = ref(false);
const expanded = ref("");

const micEnabled = computed(() => !!status.value?.stt_enabled);

async function load() {
	try {
		const [st, page] = await Promise.all([api.getBusinessStatus(), api.listVoiceNotes(0, 30)]);
		status.value = st;
		notes.value = page?.rows || [];
	} catch (e) {
		error.value = e?.message || "Couldn't load your notes.";
	} finally {
		loaded.value = true;
	}
}

async function save() {
	const text = draft.value.trim();
	if (!text || saving.value) return;
	saving.value = true;
	error.value = "";
	try {
		await api.saveVoiceNote(text);
		draft.value = "";
		await load();
	} catch (e) {
		error.value = e?.message || "Couldn't save that note.";
	} finally {
		saving.value = false;
	}
}

async function remove(name) {
	try {
		await api.deleteVoiceNote(name);
		notes.value = notes.value.filter((n) => n.name !== name);
	} catch (e) {
		error.value = e?.message || "Couldn't delete that note.";
	}
}

// Dictation drops the transcript into the draft rather than saving it outright:
// a note you can't read before it is committed is a note you can't correct.
function onTranscript(text) {
	draft.value = draft.value ? `${draft.value} ${text}` : text;
}

onMounted(load);
</script>

<template>
	<AppBar title="Business" />

	<div class="jv-scroll">
		<div class="jv-capture">
			<div class="jv-capture-head">Tell {{ agentName }} how your business works</div>
			<div class="jv-capture-sub">
				“We never ship to a customer with an overdue invoice.” Notes become defaults the
				agent applies on its own.
			</div>
			<div class="jv-capture-box">
				<textarea v-model="draft" rows="3" placeholder="Type or dictate a note…" />
				<div class="jv-capture-actions">
					<button v-if="micEnabled" class="jv-ghost-btn" @click="voiceOpen = true">
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
							<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
							<path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" />
						</svg>
						Dictate
					</button>
					<button
						class="jv-primary-btn"
						:disabled="!draft.trim() || saving"
						@click="save"
					>
						{{ saving ? "Saving…" : "Save note" }}
					</button>
				</div>
			</div>
			<div v-if="error" class="jv-err">{{ error }}</div>
		</div>

		<div v-if="status" class="jv-stats">
			<div class="jv-stat">
				<span class="jv-stat-n">{{ status.my_notes ?? 0 }}</span>
				<span class="jv-stat-l">your notes</span>
			</div>
			<div v-if="status.last_processed_at" class="jv-stat">
				<span class="jv-stat-n">{{ relativeTime(status.last_processed_at) }}</span>
				<span class="jv-stat-l">last processed</span>
			</div>
		</div>

		<div v-if="!loaded" class="jv-empty">Loading…</div>
		<div v-else-if="!notes.length" class="jv-empty" style="height: auto; padding: 24px">
			No notes yet. The first one teaches {{ agentName }} something new.
		</div>

		<ul v-else class="jv-notes">
			<li v-for="n in notes" :key="n.name" class="jv-note">
				<button class="jv-note-body" @click="expanded = expanded === n.name ? '' : n.name">
					<div class="jv-note-text" :class="{ 'is-open': expanded === n.name }">
						{{ expanded === n.name ? n.transcript : n.excerpt || n.transcript }}
					</div>
					<div class="jv-note-meta">
						<span class="jv-pill" :class="{ 'is-done': n.status === 'Processed' }">{{
							n.status
						}}</span>
						{{ relativeTime(n.creation) }}
					</div>
				</button>
				<button class="jv-note-x" aria-label="Delete note" @click="remove(n.name)">
					<svg
						viewBox="0 0 24 24"
						width="16"
						height="16"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
					>
						<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
					</svg>
				</button>
			</li>
		</ul>
	</div>

	<VoiceSheet :open="voiceOpen" @close="voiceOpen = false" @transcript="onTranscript" />
</template>

<style scoped>
.jv-capture {
	padding: 14px 12px 4px;
}
.jv-capture-head {
	font-size: 15px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-capture-sub {
	margin-top: 4px;
	font-size: 13px;
	line-height: 1.45;
	color: var(--ink5);
}
.jv-capture-box {
	margin-top: 10px;
	padding: 10px;
	border: 1px solid var(--border2);
	border-radius: 12px;
	background: var(--card);
}
.jv-capture-box textarea {
	width: 100%;
	border: 0;
	outline: none;
	resize: none;
	background: transparent;
	color: var(--ink9);
	font: inherit;
	font-size: 14.5px;
	line-height: 1.45;
}
.jv-capture-actions {
	display: flex;
	justify-content: flex-end;
	gap: 8px;
	margin-top: 6px;
}
.jv-ghost-btn {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 8px 12px;
	border: 1px solid var(--border2);
	border-radius: 9px;
	background: var(--card);
	color: var(--ink7);
	font: inherit;
	font-size: 13.5px;
	font-weight: 500;
	cursor: pointer;
}
.jv-primary-btn {
	padding: 8px 14px;
	border: 0;
	border-radius: 9px;
	background: var(--accent-solid);
	color: #fff;
	font: inherit;
	font-size: 13.5px;
	font-weight: 600;
	cursor: pointer;
}
.jv-primary-btn:disabled {
	opacity: 0.5;
}
.jv-err {
	margin-top: 8px;
	font-size: 12.5px;
	color: var(--red);
}

.jv-stats {
	display: flex;
	gap: 8px;
	padding: 12px;
}
.jv-stat {
	flex: 1;
	display: flex;
	flex-direction: column;
	gap: 2px;
	padding: 10px 12px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
}
.jv-stat-n {
	font-size: 15px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-stat-l {
	font-size: 11.5px;
	color: var(--ink5);
}

.jv-notes {
	list-style: none;
	margin: 0;
	padding: 0 8px 16px;
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.jv-note {
	display: flex;
	align-items: flex-start;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-note-body {
	flex: 1;
	min-width: 0;
	padding: 12px;
	border: 0;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-note-text {
	font-size: 14px;
	line-height: 1.45;
	color: var(--ink8);
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	overflow: hidden;
}
.jv-note-text.is-open {
	-webkit-line-clamp: unset;
	display: block;
}
.jv-note-meta {
	display: flex;
	align-items: center;
	gap: 8px;
	margin-top: 6px;
	font-size: 11.5px;
	color: var(--ink5);
}
.jv-pill {
	padding: 2px 7px;
	border-radius: 999px;
	background: var(--card2);
	color: var(--ink6);
	font-weight: 500;
}
.jv-pill.is-done {
	background: var(--green-bg);
	color: var(--green);
}
.jv-note-x {
	flex: none;
	padding: 12px 12px 12px 8px;
	border: 0;
	background: transparent;
	color: var(--ink4);
	cursor: pointer;
}
</style>
