<template>
	<Dropdown :options="menuOptions">
		<template #trigger="{ open }">
			<button
				class="jv-focus-ring relative flex h-12 items-center rounded-md py-2 duration-300 ease-in-out"
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
				<!-- Resting badge: a waiting reply has to register on every route, so it
				     lives here on the avatar - the SOLE unread indicator now (the
				     chat-header jv-support-dot was removed; that control is new-ticket
				     only). Red + pulse to actually pull the eye: an unheard reply is a
				     pending action ON the user, the same semantic (bg-surface-red-5) as
				     Sidebar.vue's approvals dot. motion-safe so reduced-motion stays calm. -->
				<div
					v-if="supportOn && store.awaitingCount"
					class="absolute left-7 top-1 size-2 rounded-full bg-surface-red-5 motion-safe:animate-pulse"
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
// by this poll timer so the avatar's resting badge and the ticket list always
// read the same awaiting-count value without each needing to run its own poller.
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
			// Support tickets -> the LIST (/support). Distinct from ChatView's header
			// headset icon, which opens a NEW ticket pre-filled with the current chat;
			// this entry is the way back to existing tickets. Chat variant only (the
			// support rail reaches its own list), and gated on supportOn because the
			// /support routes sit behind supportGuard on the same flags — so it is
			// never a dead link that bounces the user back to Chat.
			...(props.variant === "support" || !supportOn
				? []
				: [
						{
							// Count flag mirrors the avatar's resting dot, so opening the
							// menu confirms where the waiting reply is.
							label: store.awaitingCount
								? `Support tickets · ${store.awaitingCount}`
								: "Support tickets",
							icon: "life-buoy",
							onClick: () => router.push({ name: "Support" }),
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
