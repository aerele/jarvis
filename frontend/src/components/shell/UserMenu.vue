<template>
	<Dropdown :options="menuOptions">
		<template #trigger="{ open }">
			<button
				class="relative flex h-12 items-center rounded-md py-2 duration-300 ease-in-out focus:outline-none focus:transition-none focus-visible:[outline:2px_solid_var(--focus-ring)] focus-visible:[outline-offset:2px]"
				:class="
					isCollapsed
						? 'w-auto px-0'
						: open
						? 'w-full px-2 bg-surface-white shadow-sm'
						: 'w-full px-2 hover:bg-surface-gray-3'
				"
				:aria-label="`${cardTitle} menu`"
				@mouseenter="brandPeek = true"
				@mouseleave="brandPeek = false"
			>
				<!-- the jarvis mark, 28×28 rounded — rendered from JarvisMark rather than
				     a hand-pasted copy of its gradient + path data. That duplication is
				     exactly what let the chat welcome mark drift to a different colour
				     (design.md §2.2). idlePeek: this is the one mark that sits resting on
				     every route, so it's the chosen surface for the designed idle blink. -->
				<JarvisMark :size="28" :radius="7" :peek="brandPeek" idlePeek />
				<!-- Resting badge: a waiting reply has to register on every route, not
				     only while the user happens to be in Chat - restoring this (it was
				     briefly dropped when ChatView grew its own header jv-support-dot,
				     which stayed too: both showing the same thing on the chat route is
				     harmless duplication, losing it everywhere else was not). Mirrors
				     the collapsed-sidebar dot in Sidebar.vue:57-62. -->
				<div
					v-if="supportOn && store.awaitingCount"
					class="absolute left-7 top-1 size-2 rounded-full bg-surface-amber-2"
					:aria-label="`${store.awaitingCount} ${
						store.awaitingCount === 1 ? 'ticket' : 'tickets'
					} awaiting your reply`"
					role="status"
				/>
				<div
					class="flex flex-1 flex-col overflow-hidden text-left duration-300 ease-in-out"
					:class="isCollapsed ? 'ml-0 w-0 opacity-0' : 'ml-2 w-auto opacity-100'"
				>
					<div class="truncate text-base font-medium leading-none text-ink-gray-9">
						{{ cardTitle }}
					</div>
					<div class="mt-1 truncate text-sm text-ink-gray-7">{{ fullName }}</div>
				</div>
				<FeatherIcon
					v-if="!isCollapsed"
					name="chevron-down"
					class="h-4 w-4 shrink-0 text-ink-gray-5"
				/>
			</button>
		</template>
	</Dropdown>
</template>

<script setup>
// Sidebar header (DESIGN-V3 §3.2.1): brand + session user, HD's UserMenu
// pattern. Dropdown: Settings (D9) · Switch to Desk · Change theme · Log out.
import { computed, inject, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Dropdown, FeatherIcon } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { useShellStore } from "@/stores/shell";
import { useSupportStore } from "@/stores/support";
import { useJarvisTheme } from "@/theme";
import { agentName } from "@/branding";

const props = defineProps({
	isCollapsed: { type: Boolean, default: false },
	// "chat" (default) = the Jarvis card + a "Support" link; "support" = a
	// "Jarvis Support" title + a "Switch to Jarvis chat" link (we're already in support).
	variant: { type: String, default: "chat" },
});

// Whole-card peek: hovering anywhere on the brand button (mark + name), not just
// the mark itself, reveals the blinking eyes. Two tiny handlers because the
// hover surface is the parent button, wider than the mark JarvisMark owns.
const brandPeek = ref(false);

// Card title: agentName ("Jarvis") in chat; "<agentName> Support" on the support rail.
const cardTitle = computed(() =>
	props.variant === "support" ? `${agentName} Support` : agentName
);

const shellStore = useShellStore();
const session = inject("$session");
const { effectiveDark, toggleTheme } = useJarvisTheme();

// Support panel: `store` is the shared support-store singleton, kept fresh here
// by this poll timer so ChatView's header Support button (its jv-support-dot)
// and the ticket list always read the same awaiting-count value without each
// needing to run its own poller.
const router = useRouter();
const supportOn = window.support_available && window.has_support_access;
const store = useSupportStore();
let pollTimer = null;
async function pollAwaiting() {
	if (document.hidden) return;
	await store.refreshAwaiting();
}
onMounted(() => {
	if (!supportOn) return;
	pollAwaiting();
	pollTimer = setInterval(pollAwaiting, 60000);
});
onUnmounted(() => {
	if (pollTimer) clearInterval(pollTimer);
});

function cookie(name) {
	// URLSearchParams already percent-decodes; decoding AGAIN throws URIError
	// when the display name contains a literal '%' (stored as %25 → '%'),
	// blanking the whole shell — same bug main fixed in lib/user.js (0d19e7c).
	return new URLSearchParams(document.cookie.split("; ").join("&")).get(name);
}
const fullName = cookie("full_name") || session.user || "User";

// Cross-surface link: from the support rail -> back to chat (we're already in
// support, so a "Support" link is pointless there). The chat-side "Support" entry
// used to live here too, but it now lives in ChatView's header icon button
// instead - keeping both would show Support twice on the chat side.
const crossItem = computed(() => {
	if (props.variant === "support") {
		return {
			label: `Switch to ${agentName} chat`,
			icon: "message-circle",
			onClick: () => router.push({ name: "Chat" }),
		};
	}
	return null;
});

const menuOptions = computed(() => [
	{
		group: "Menu",
		hideLabel: true,
		items: [
			// The app/LLM Settings dialog is an admin/chat concern — omit it on the
			// customer support rail (variant "support").
			...(props.variant === "support"
				? []
				: [
						{
							label: "Settings",
							icon: "settings",
							onClick: () => shellStore.openSettings(),
						},
				  ]),
			...(crossItem.value ? [crossItem.value] : []),
			{
				label: "Switch to Desk",
				icon: "grid",
				onClick: () => {
					window.location.href = "/app";
				},
			},
			{
				label: "Change theme",
				icon: effectiveDark.value ? "sun" : "moon",
				onClick: () => toggleTheme(),
			},
		],
	},
	{
		group: "Danger",
		hideLabel: true,
		items: [
			{ label: "Log out", icon: "log-out", theme: "red", onClick: () => session.logout() },
		],
	},
]);
</script>
