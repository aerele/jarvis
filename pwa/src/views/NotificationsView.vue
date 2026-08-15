<script setup>
import { computed } from "vue";
import { useRouter } from "vue-router";
import { store } from "../store";
import { feed, markAllRead, markRead } from "../lib/notifications";
import { agentName } from "@/branding";

// What Jarvis did while you were somewhere else. Split New / Earlier, exactly as
// the native app does — an unread task is a thing to act on, a read one is
// history, and mixing them makes both harder to read.
const router = useRouter();

const fresh = computed(() => feed.items.filter((i) => !i.read));
const earlier = computed(() => feed.items.filter((i) => i.read));

const VISUAL = {
	"task-finished": { cls: "is-green", icon: "M20 6 9 17l-5-5" },
	"needs-decision": {
		cls: "is-amber",
		icon: "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01",
	},
	"task-failed": { cls: "is-red", icon: "M18 6 6 18M6 6l12 12" },
	"new-conversation": {
		cls: "is-accent",
		icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
	},
};
const visual = (kind) => VISUAL[kind] || VISUAL["new-conversation"];

function relTime(at) {
	const diff = (Date.now() - at) / 1000;
	if (diff < 60) return "now";
	if (diff < 3600) return `${Math.floor(diff / 60)}m`;
	if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
	if (diff < 604800) return `${Math.floor(diff / 86400)}d`;
	return new Date(at).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// A notification is a pointer, not a place: tapping it takes you to the chat it
// came from. An approval in particular can only be answered there.
function open(item) {
	markRead(item.id);
	if (item.conversation) router.push(`/c/${item.conversation}`);
	else router.push("/");
}
</script>

<template>
	<div class="jv-bar">
		<button class="jv-icon-btn" aria-label="Back" @click="router.back()">
			<svg
				viewBox="0 0 24 24"
				width="20"
				height="20"
				fill="none"
				stroke="currentColor"
				stroke-width="1.9"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m15 18-6-6 6-6" />
			</svg>
		</button>
		<div class="jv-title">Notifications</div>
		<button v-if="feed.unread" class="jv-markall" @click="markAllRead">Mark all read</button>
	</div>

	<div class="jv-scroll jv-pad">
		<div v-if="!feed.items.length" class="jv-empty">
			<span class="jv-empty-icon">
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
					<path
						d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"
					/>
				</svg>
			</span>
			<div style="font-size: 15px; font-weight: 600; color: var(--ink9)">Nothing yet</div>
			<div style="font-size: 14px; line-height: 1.5">
				Finished tasks, decisions {{ agentName }} needs and new conversations land here.
			</div>
		</div>

		<template v-else>
			<template v-if="fresh.length">
				<div class="jv-label">New</div>
				<button v-for="n in fresh" :key="n.id" class="jv-notif" @click="open(n)">
					<span class="jv-notif-icon" :class="visual(n.kind).cls">
						<svg
							viewBox="0 0 24 24"
							width="17"
							height="17"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path :d="visual(n.kind).icon" />
						</svg>
						<span class="jv-unread" />
					</span>
					<span class="jv-notif-main">
						<span class="jv-notif-head">
							<span class="jv-notif-title">{{ n.title }}</span>
							<span class="jv-notif-time">{{ relTime(n.at) }}</span>
						</span>
						<span class="jv-notif-body">{{ n.body }}</span>
						<span v-if="n.conversation" class="jv-notif-cta">
							Open chat
							<svg
								viewBox="0 0 24 24"
								width="13"
								height="13"
								fill="none"
								stroke="currentColor"
								stroke-width="2.2"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="m9 18 6-6-6-6" />
							</svg>
						</span>
					</span>
				</button>
			</template>

			<template v-if="earlier.length">
				<div class="jv-label">Earlier</div>
				<button
					v-for="n in earlier"
					:key="n.id"
					class="jv-notif is-earlier"
					@click="open(n)"
				>
					<span class="jv-notif-icon" :class="visual(n.kind).cls">
						<svg
							viewBox="0 0 24 24"
							width="17"
							height="17"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path :d="visual(n.kind).icon" />
						</svg>
					</span>
					<span class="jv-notif-main">
						<span class="jv-notif-head">
							<span class="jv-notif-title">{{ n.title }}</span>
							<span class="jv-notif-time">{{ relTime(n.at) }}</span>
						</span>
						<span class="jv-notif-body">{{ n.body }}</span>
					</span>
				</button>
			</template>
		</template>
	</div>
</template>

<style scoped>
.jv-markall {
	flex: none;
	padding: 8px 11px;
	border: 0;
	background: transparent;
	color: var(--accent);
	font: inherit;
	font-size: 12.5px;
	font-weight: 500;
	cursor: pointer;
}
.jv-pad {
	padding: 4px 12px 40px;
}
.jv-label {
	margin: 8px 6px;
	font-size: 11px;
	font-weight: 600;
	letter-spacing: 0.6px;
	text-transform: uppercase;
	color: var(--ink5);
}
.jv-notif {
	display: flex;
	align-items: flex-start;
	gap: 12px;
	width: 100%;
	padding: 12px 10px;
	border: 0;
	border-radius: 12px;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-notif:active {
	background: var(--card2);
}
.jv-notif.is-earlier {
	opacity: 0.85;
}
.jv-notif-icon {
	position: relative;
	display: grid;
	place-items: center;
	width: 36px;
	height: 36px;
	flex: none;
	border-radius: 10px;
}
.jv-notif-icon.is-green {
	background: var(--green-bg);
	color: var(--green);
}
.jv-notif-icon.is-amber {
	background: var(--amber-bg);
	color: var(--amber);
}
.jv-notif-icon.is-red {
	background: var(--red-bg);
	color: var(--red);
}
.jv-notif-icon.is-accent {
	background: var(--accent-bg);
	color: var(--accent);
}
.jv-unread {
	position: absolute;
	top: -3px;
	right: -3px;
	width: 10px;
	height: 10px;
	border-radius: 999px;
	background: var(--accent);
	border: 2px solid var(--bg);
}
.jv-notif-main {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-notif-head {
	display: flex;
	align-items: baseline;
	gap: 8px;
}
.jv-notif-title {
	flex: 1;
	min-width: 0;
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-notif-time {
	flex: none;
	font-size: 11px;
	font-variant-numeric: tabular-nums;
	color: var(--ink4);
}
.jv-notif-body {
	margin-top: 2px;
	font-size: 12.5px;
	line-height: 1.4;
	color: var(--ink6);
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	overflow: hidden;
}
.jv-notif-cta {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	align-self: flex-start;
	margin-top: 8px;
	height: 26px;
	padding: 0 12px;
	border-radius: 999px;
	background: var(--card3);
	color: var(--ink8);
	font-size: 12px;
	font-weight: 600;
}
.jv-empty-icon {
	display: grid;
	place-items: center;
	width: 48px;
	height: 48px;
	border-radius: 14px;
	background: var(--card2);
	color: var(--ink5);
}
</style>
