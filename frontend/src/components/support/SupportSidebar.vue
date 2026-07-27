<template>
	<nav class="jv-supsb" :class="{ 'jv-supsb-collapsed': collapsed }">
		<!-- The support left nav rail (Helpdesk AppSidebar, minimal). Painted in
		     frappe-ui semantic tokens (like the app header) — it sits OUTSIDE the
		     chat-surface jv-root, so it must not depend on the jv palette vars.
		     (Comment kept inside the root to avoid a multi-root fragment.) -->
		<div class="jv-supsb-top">
			<!-- The same brand + user card as the chat sidebar (JarvisMark + Jarvis /
			     full name + a Settings/Support/Desk/Theme/Log-out dropdown), reused
			     verbatim so it stays identical. Chat navigation is the "Jarvis chat"
			     nav link below. -->
			<UserMenu :is-collapsed="collapsed" />

			<div class="jv-supsb-nav">
				<SidebarLink
					icon="plus"
					label="New ticket"
					:to="{ name: 'SupportNew' }"
					:is-collapsed="collapsed"
				/>
				<SidebarLink
					icon="inbox"
					label="Support tickets"
					:to="{ name: 'Support' }"
					:is-collapsed="collapsed"
					:is-active="isTickets"
				/>
				<SidebarLink
					icon="message-circle"
					label="Jarvis chat"
					:to="{ name: 'Chat' }"
					:is-collapsed="collapsed"
				/>
			</div>
		</div>

		<div class="jv-supsb-bottom">
			<SidebarLink
				icon="external-link"
				label="Open ERPNext Desk"
				:on-click="openDesk"
				:is-collapsed="collapsed"
			/>
			<SidebarLink
				data-test="collapse-toggle"
				:icon="collapsed ? 'chevrons-right' : 'chevrons-left'"
				:label="collapsed ? 'Expand' : 'Collapse'"
				:on-click="toggle"
				:is-collapsed="collapsed"
			/>
		</div>
	</nav>
</template>

<script setup>
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import UserMenu from "@/components/shell/UserMenu.vue";
import SidebarLink from "@/components/shell/SidebarLink.vue";

const route = useRoute();

// Collapse state persists across reloads, self-contained (the shell doesn't need
// to know — the rail sets its own width and SidebarLink hides labels when collapsed).
const KEY = "jv-support-sidebar-collapsed";
const collapsed = ref(readCollapsed());
function readCollapsed() {
	try {
		return localStorage.getItem(KEY) === "1";
	} catch {
		return false;
	}
}
function toggle() {
	collapsed.value = !collapsed.value;
	try {
		localStorage.setItem(KEY, collapsed.value ? "1" : "0");
	} catch {
		/* private mode / storage disabled — stay in-memory */
	}
}

// "Support tickets" is active across every support page (list/new/thread); "Jarvis
// chat" is the exit link, never active while we're on a support route.
const isTickets = computed(() => route.path.startsWith("/support"));

function openDesk() {
	window.open("/app", "_blank");
}
</script>

<style scoped>
.jv-supsb {
	display: flex;
	flex-direction: column;
	justify-content: space-between;
	flex: 0 0 auto;
	width: 220px;
	padding: 12px 10px;
	border-right: 1px solid var(--outline-gray-1);
	background: var(--surface-white);
	overflow: hidden;
	transition: width 0.18s ease;
}
.jv-supsb-collapsed {
	width: 56px;
}
.jv-supsb-top {
	display: flex;
	flex-direction: column;
	min-height: 0;
}
.jv-supsb-nav,
.jv-supsb-bottom {
	display: flex;
	flex-direction: column;
	gap: 2px;
}
.jv-supsb-nav {
	margin-top: 14px;
}
</style>
