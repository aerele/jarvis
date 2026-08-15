<script setup>
import { computed, onMounted, ref } from "vue";
import BrandMark from "../components/BrandMark.vue";
import { agentName } from "@/branding";
import { useRouter } from "vue-router";
import { store } from "../store";
import { relativeTime } from "../lib/time";

const router = useRouter();
const search = ref("");

// New chat = navigate to the thread with no id. send_message creates (or
// focuses) the empty conversation server-side on the first send, so we don't
// spend a round-trip creating one the user might never type into.
function newChat() {
	router.push("/c/new");
}

// Starred chats pin to the top — the same order list_conversations returns, kept
// explicitly so a client-side filter can't quietly reshuffle it.
const rows = computed(() => {
	const q = search.value.trim().toLowerCase();
	const list = q
		? store.conversations.filter((c) => (c.title || "New chat").toLowerCase().includes(q))
		: store.conversations;
	return [...list].sort((a, b) => (b.starred || 0) - (a.starred || 0));
});

const isUnread = (c) => store.unread.has(c.name);

// `last_active_at` — NOT `modified`. list_conversations doesn't return
// `modified`, so reading it (as this screen used to) printed an empty timestamp
// under every single chat.
const subtitle = (c) => {
	const when = relativeTime(c.last_active_at);
	const n = Number(c.message_count || 0);
	if (!n) return when || "Empty chat";
	return [when, `${n} message${n === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
};

onMounted(() => {
	if (!store.loaded) store.loadConversations();
});
</script>

<template>
	<div class="jv-bar">
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
		<div class="jv-title">Chats</div>
		<button class="jv-icon-btn" aria-label="New chat" @click="newChat">
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
				<path d="M12 5v14M5 12h14" />
			</svg>
		</button>
	</div>

	<div v-if="store.conversations.length > 4" class="jv-searchbar">
		<svg
			viewBox="0 0 24 24"
			width="16"
			height="16"
			fill="none"
			stroke="currentColor"
			stroke-width="1.9"
			stroke-linecap="round"
		>
			<circle cx="11" cy="11" r="7" />
			<path d="m21 21-4.3-4.3" />
		</svg>
		<input v-model="search" placeholder="Search chats" />
	</div>

	<div class="jv-scroll">
		<div v-if="!store.loaded" class="jv-empty">Loading…</div>

		<div v-else-if="!store.conversations.length" class="jv-empty">
			<BrandMark :size="52" />
			<div style="font-size: 16px; font-weight: 600; color: var(--ink9)">
				Ask {{ agentName }} anything
			</div>
			<div style="font-size: 14px; line-height: 1.5">
				Invoices, stock, customers, reports, all in plain language. Start a chat and see.
			</div>
		</div>

		<div v-else-if="!rows.length" class="jv-empty">No chat matches “{{ search }}”.</div>

		<ul v-else class="jv-list">
			<li v-for="c in rows" :key="c.name">
				<button class="jv-row" @click="router.push(`/c/${c.name}`)">
					<div class="jv-row-main">
						<div class="jv-row-title">
							<svg
								v-if="c.starred"
								class="jv-star"
								viewBox="0 0 24 24"
								width="13"
								height="13"
								fill="currentColor"
							>
								<path
									d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"
								/>
							</svg>
							{{ c.title || "New chat" }}
						</div>
						<div class="jv-row-time">{{ subtitle(c) }}</div>
					</div>
					<!-- Reserved lane, empty on read rows: the chevron must not shift
					     column when a neighbour gains a dot. -->
					<span
						class="jv-row-dot"
						:class="{ 'is-unread': isUnread(c) }"
						:role="isUnread(c) ? 'img' : undefined"
						:aria-label="isUnread(c) ? 'New reply' : undefined"
					/>
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
.jv-searchbar {
	display: flex;
	align-items: center;
	gap: 8px;
	flex: none;
	margin: 8px 12px 0;
	padding: 0 12px;
	height: 40px;
	border: 1px solid var(--border2);
	border-radius: 12px;
	background: var(--card);
	color: var(--ink4);
}
.jv-searchbar input {
	flex: 1;
	min-width: 0;
	border: 0;
	outline: none;
	background: transparent;
	color: var(--ink9);
	font: inherit;
	font-size: 14.5px;
}

.jv-list {
	list-style: none;
	margin: 0;
	padding: 8px;
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.jv-row {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	padding: 14px 12px;
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
	display: flex;
	align-items: center;
	gap: 6px;
	font-size: 15px;
	font-weight: 500;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-star {
	flex: none;
	color: var(--amber-dot);
}
.jv-row-time {
	margin-top: 3px;
	font-size: 12px;
	color: var(--ink5);
}
/* Unread marker. The lane is always present so the chevron holds its column;
   only the fill appears. Solid accent, no pulse: a pulse reads as "working",
   and this reply already finished. */
.jv-row-dot {
	width: 8px;
	height: 8px;
	flex: none;
	border-radius: 50%;
	background: transparent;
}
.jv-row-dot.is-unread {
	background: var(--accent-solid);
}
.jv-row-chev {
	width: 16px;
	height: 16px;
	flex: none;
	color: var(--ink3);
}
</style>
