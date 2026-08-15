<script setup>
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import AppBar from "../components/AppBar.vue";
import * as api from "../api";
import { store } from "../store";
import { relativeTime } from "../lib/time";

// File Box: drop a document and get back a chat that has already read it.
// This is the screen that most wants to be on a phone — the invoice arrives as a
// photo, and the desk is somewhere else.
const router = useRouter();

const rows = ref([]);
const loaded = ref(false);
const busy = ref(false);
const error = ref("");
const fileEl = ref(null);

async function load() {
	try {
		const page = await api.listInbound(0, 30);
		rows.value = page?.rows || [];
	} catch (e) {
		error.value = e?.message || "Couldn't load the file box.";
	} finally {
		loaded.value = true;
	}
}

async function pick(e) {
	const files = [...(e.target.files || [])];
	e.target.value = "";
	if (!files.length || busy.value) return;

	busy.value = true;
	error.value = "";
	try {
		// Upload, then hand the file to the agent. drop_file opens (and starts) a
		// conversation about it, so go straight there — the processing IS the chat.
		const up = await api.uploadFile(files[0]);
		const r = await api.dropFile(up.file_url, up.file_name);
		if (r?.ok === false) {
			error.value = r.reason || "Jarvis couldn't take that file.";
			return;
		}
		store.loadConversations();
		if (r?.conversation_id) router.push(`/c/${r.conversation_id}`);
		else await load();
	} catch (e) {
		error.value = e?.message || "Couldn't upload that file.";
	} finally {
		busy.value = false;
	}
}

onMounted(load);
</script>

<template>
	<AppBar title="File Box" />

	<div class="jv-scroll">
		<div class="jv-drop">
			<div class="jv-drop-icon">
				<svg
					viewBox="0 0 24 24"
					width="22"
					height="22"
					fill="none"
					stroke="currentColor"
					stroke-width="1.7"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M22 12h-6l-2 3h-4l-2-3H2" />
					<path
						d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"
					/>
				</svg>
			</div>
			<div class="jv-drop-text">
				<strong>Drop a document</strong>
				<span
					>A bill, a PO, a photo of a receipt. Jarvis reads it and starts a chat about
					it.</span
				>
			</div>
			<input ref="fileEl" type="file" hidden @change="pick" />
			<button class="jv-primary-btn" :disabled="busy" @click="fileEl.click()">
				{{ busy ? "Uploading…" : "Choose file" }}
			</button>
		</div>

		<div v-if="error" class="jv-err">{{ error }}</div>

		<div v-if="!loaded" class="jv-empty">Loading…</div>
		<div v-else-if="!rows.length" class="jv-empty" style="height: auto; padding: 24px">
			Nothing dropped yet.
		</div>

		<ul v-else class="jv-list">
			<li v-for="r in rows" :key="r.name">
				<button class="jv-row" @click="router.push(`/c/${r.name}`)">
					<div class="jv-row-main">
						<div class="jv-row-title">{{ r.title || "Untitled document" }}</div>
						<div class="jv-row-sub">
							<span
								class="jv-pill"
								:class="{
									'is-done': /done|complete|processed/i.test(r.status || ''),
								}"
							>
								{{ r.status || "Pending" }}
							</span>
							{{ relativeTime(r.creation) }}
						</div>
					</div>
					<svg
						class="jv-row-chev"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="m9 18 6-6-6-6" />
					</svg>
				</button>
			</li>
		</ul>
	</div>
</template>

<style scoped>
.jv-drop {
	display: flex;
	align-items: center;
	gap: 12px;
	margin: 12px;
	padding: 14px 12px;
	border: 1px dashed var(--border2);
	border-radius: 14px;
	background: var(--card);
}
.jv-drop-icon {
	display: grid;
	place-items: center;
	width: 42px;
	height: 42px;
	flex: none;
	border-radius: 11px;
	background: var(--accent-bg);
	color: var(--accent);
}
.jv-drop-text {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 2px;
	font-size: 12.5px;
	line-height: 1.4;
	color: var(--ink5);
}
.jv-drop-text strong {
	font-size: 14px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-primary-btn {
	flex: none;
	padding: 9px 13px;
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
	opacity: 0.55;
}
.jv-err {
	margin: 0 12px;
	font-size: 12.5px;
	color: var(--red);
}

.jv-list {
	list-style: none;
	margin: 0;
	padding: 4px 8px 16px;
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.jv-row {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	padding: 13px 12px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-row:active {
	background: var(--card2);
}
.jv-row-main {
	flex: 1;
	min-width: 0;
}
.jv-row-title {
	font-size: 14.5px;
	font-weight: 500;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-row-sub {
	display: flex;
	align-items: center;
	gap: 8px;
	margin-top: 4px;
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
.jv-row-chev {
	width: 16px;
	height: 16px;
	flex: none;
	color: var(--ink3);
}
</style>
