<template>
	<Dropdown :options="menuOptions">
		<template #trigger="{ open }">
			<button
				class="flex h-12 items-center rounded-md py-2 duration-300 ease-in-out"
				:class="
					isCollapsed
						? 'w-auto px-0'
						: open
						? 'w-full px-2 bg-surface-white shadow-sm'
						: 'w-full px-2 hover:bg-surface-gray-3'
				"
				:aria-label="`${agentName} menu`"
			>
				<!-- the jarvis mark, 28×28 rounded — rendered from JarvisMark rather than
				     a hand-pasted copy of its gradient + path data. That duplication is
				     exactly what let the chat welcome mark drift to a different colour
				     (design.md §2.2). -->
				<JarvisMark :size="28" :radius="7" />
				<div
					class="flex flex-1 flex-col overflow-hidden text-left duration-300 ease-in-out"
					:class="isCollapsed ? 'ml-0 w-0 opacity-0' : 'ml-2 w-auto opacity-100'"
				>
					<div class="truncate text-base font-medium leading-none text-ink-gray-9">
						{{ agentName }}
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
import { computed, inject, ref, onMounted, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { Dropdown, FeatherIcon } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { useShellStore } from "@/stores/shell";
import { useJarvisTheme } from "@/theme";
import { supportAwaitingCount } from "@/api";
import { agentName } from "@/branding";

defineProps({
	isCollapsed: { type: Boolean, default: false },
});

const store = useShellStore();
const session = inject("$session");
const { effectiveDark, toggleTheme } = useJarvisTheme();

// Support panel (Plan 3 C3/C4): dual kill-switch + an SPA-driven awaiting-count badge.
const router = useRouter();
const supportOn = window.support_available && window.has_support_access;
const awaiting = ref(0);
let pollTimer = null;
async function pollAwaiting() {
	try {
		const r = await supportAwaitingCount();
		awaiting.value = (r.data && r.data.count) || 0;
	} catch (e) {
		/* support is best-effort — never surface a boot error */
	}
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

const menuOptions = computed(() => [
	{
		group: "Menu",
		hideLabel: true,
		items: [
			{ label: "Settings", icon: "settings", onClick: () => store.openSettings() },
			...(supportOn
				? [
						{
							label: awaiting.value ? `Support (${awaiting.value})` : "Support",
							icon: "life-buoy",
							onClick: () => router.push({ name: "Support" }),
						},
				  ]
				: []),
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
