<script setup>
import { computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { store } from "../store";

// The native app's drawer, one for one (jarvis_mobile Drawer.tsx): four
// destinations, then your starred and recent chats, and a bottom bar with your
// profile and a gear. Nothing else — macros, approvals, agents and the wiki are
// desk work, and a link out to the desktop workspace is not a phone feature.
const router = useRouter();
const route = useRoute();

const user = computed(() => window.frappe_user_id || "");
const fullName = computed(() => window.frappe_full_name || user.value);
const initials = computed(
	() =>
		(fullName.value || user.value || "?")
			.split(/\s+/)
			.map((w) => w[0])
			.slice(0, 2)
			.join("")
			.toUpperCase() || "?"
);

const NAV = [
	{ label: "New chat", to: "/c/new", icon: "M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" },
	{
		label: "Chats",
		to: "/",
		icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
	},
	{
		label: "Business",
		to: "/business",
		icon: "M20 7H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2zM16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16",
	},
	{
		label: "File Box",
		to: "/files",
		icon: "M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
	},
];

const isActive = (l) =>
	l.to === "/"
		? route.path === "/"
		: l.to === "/c/new"
		? route.path === "/c/new"
		: route.path.startsWith(l.to);

// The chats you actually reach for. Starred first, then recent — the same 6/8
// split the native drawer uses, because a phone drawer is a shortcut, not a list.
const starred = computed(() => store.conversations.filter((c) => c.starred).slice(0, 6));
const recent = computed(() => store.conversations.filter((c) => !c.starred).slice(0, 8));

// Load the list when the drawer opens, not on every screen: the drawer is the
// only thing here that needs it.
watch(
	() => store.drawerOpen,
	(open) => {
		if (open && !store.loaded) store.loadConversations();
	}
);

function go(to) {
	store.drawerOpen = false;
	if (route.fullPath !== to) router.push(to);
}
</script>

<template>
	<Transition name="jv-drawer">
		<div v-if="store.drawerOpen" class="jv-drawer-root">
			<div class="jv-scrim" @click="store.drawerOpen = false" />
			<aside class="jv-drawer jv-safe-top">
				<nav class="jv-drawer-nav">
					<button
						v-for="l in NAV"
						:key="l.to"
						class="jv-nav"
						:class="{ 'is-active': isActive(l) }"
						@click="go(l.to)"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path :d="l.icon" />
						</svg>
						{{ l.label }}
					</button>

					<template v-if="starred.length">
						<div class="jv-drawer-label">Starred</div>
						<button
							v-for="c in starred"
							:key="c.name"
							class="jv-chat"
							@click="go(`/c/${c.name}`)"
						>
							<svg
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path
									d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
								/>
							</svg>
							<span>{{ c.title || "New chat" }}</span>
							<svg
								class="jv-star"
								viewBox="0 0 24 24"
								width="12"
								height="12"
								fill="currentColor"
							>
								<path
									d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"
								/>
							</svg>
						</button>
					</template>

					<template v-if="recent.length">
						<div class="jv-drawer-label">Recent</div>
						<button
							v-for="c in recent"
							:key="c.name"
							class="jv-chat"
							@click="go(`/c/${c.name}`)"
						>
							<svg
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path
									d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
								/>
							</svg>
							<span>{{ c.title || "New chat" }}</span>
						</button>
					</template>
				</nav>

				<div class="jv-drawer-foot jv-safe-bottom">
					<button class="jv-me" @click="go('/account')">
						<span class="jv-avatar">{{ initials }}</span>
						<span class="jv-me-text">
							<span class="jv-me-name">{{ fullName }}</span>
							<span class="jv-me-mail">{{ user }}</span>
						</span>
					</button>
					<button class="jv-gear" aria-label="Settings" @click="go('/settings')">
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
							<circle cx="12" cy="12" r="3" />
							<path
								d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 8.9 19a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 5 8.9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"
							/>
						</svg>
					</button>
				</div>
			</aside>
		</div>
	</Transition>
</template>

<style scoped>
.jv-drawer-root {
	position: fixed;
	inset: 0;
	z-index: 40;
}
.jv-scrim {
	position: absolute;
	inset: 0;
	background: var(--scrim);
}
.jv-drawer {
	position: absolute;
	inset: 0 auto 0 0;
	width: 270px;
	max-width: 84vw;
	display: flex;
	flex-direction: column;
	background: var(--menu-bar);
	border-right: 1px solid var(--border);
	box-shadow: 2px 0 24px rgba(0, 0, 0, 0.18);
}

.jv-drawer-nav {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 14px 10px 10px;
}
.jv-nav {
	display: flex;
	align-items: center;
	gap: 12px;
	width: 100%;
	height: 42px;
	padding: 0 12px;
	border: 0;
	border-radius: 9px;
	background: transparent;
	color: var(--ink7);
	font: inherit;
	font-size: 14px;
	text-align: left;
	cursor: pointer;
}
.jv-nav:active {
	background: var(--card2);
}
.jv-nav.is-active {
	background: var(--card3);
	color: var(--ink9);
	font-weight: 600;
}
.jv-nav svg {
	width: 17px;
	height: 17px;
	flex: none;
	color: var(--ink6);
}
.jv-nav.is-active svg {
	color: var(--ink9);
}

.jv-drawer-label {
	margin: 16px 12px 4px;
	font-size: 11px;
	font-weight: 600;
	letter-spacing: 0.6px;
	text-transform: uppercase;
	color: var(--ink5);
}
.jv-chat {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	height: 38px;
	padding: 0 12px;
	border: 0;
	border-radius: 9px;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-chat:active {
	background: var(--card2);
}
.jv-chat svg {
	width: 14px;
	height: 14px;
	flex: none;
	color: var(--ink5);
}
.jv-chat span {
	flex: 1;
	min-width: 0;
	font-size: 13px;
	color: var(--ink7);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-star {
	color: var(--amber-dot);
}

.jv-drawer-foot {
	display: flex;
	align-items: center;
	gap: 8px;
	flex: none;
	padding: 8px 10px;
	border-top: 1px solid var(--border);
}
.jv-me {
	flex: 1;
	min-width: 0;
	display: flex;
	align-items: center;
	gap: 10px;
	padding: 6px;
	border: 0;
	border-radius: 10px;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-me:active {
	background: var(--card2);
}
.jv-avatar {
	display: grid;
	place-items: center;
	width: 34px;
	height: 34px;
	flex: none;
	border-radius: 9px;
	background: var(--accent);
	color: #fff;
	font-size: 13px;
	font-weight: 600;
}
.jv-me-text {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-me-name {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-me-mail {
	font-size: 11.5px;
	color: var(--ink5);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-gear {
	display: grid;
	place-items: center;
	width: 38px;
	height: 38px;
	flex: none;
	border: 0;
	border-radius: 9px;
	background: transparent;
	color: var(--ink6);
	cursor: pointer;
}
.jv-gear:active {
	background: var(--card2);
}

.jv-drawer-enter-active,
.jv-drawer-leave-active {
	transition: opacity 0.18s ease;
}
.jv-drawer-enter-active .jv-drawer,
.jv-drawer-leave-active .jv-drawer {
	transition: transform 0.24s cubic-bezier(0.32, 0.72, 0, 1);
}
.jv-drawer-enter-from,
.jv-drawer-leave-to {
	opacity: 0;
}
.jv-drawer-enter-from .jv-drawer,
.jv-drawer-leave-to .jv-drawer {
	transform: translateX(-100%);
}
@media (prefers-reduced-motion: reduce) {
	.jv-drawer-enter-active .jv-drawer,
	.jv-drawer-leave-active .jv-drawer {
		transition: none;
	}
}
</style>
