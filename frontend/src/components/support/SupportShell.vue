<template>
	<div class="jv-sup">
		<!-- The standalone support chrome: a left nav rail + a main column (a
		     frappe-ui-tokened top bar over the page body). On chat-surface pages
		     (thread, new) the content row is wrapped as a jv-root so the chat
		     Message/Composer and the details panel render in the jv palette; the list
		     page renders bare so its frappe-ui ListPage keeps its stock look (the
		     index.css forms reset would otherwise strip its search box). A leading
		     comment must stay INSIDE the root (a comment sibling of the root compiles
		     to a multi-root fragment -> wrapper.element resolves to the mount container). -->
		<SupportSidebar />

		<div class="jv-sup-main">
			<header class="jv-sup-bar">
				<Breadcrumbs class="min-w-0" :items="crumbs" />
				<div class="jv-sup-right">
					<slot name="actions" />
				</div>
			</header>

			<div
				class="jv-sup-content"
				:class="{ 'jv-root': chatSurface, 'jv-dark': chatSurface && effectiveDark }"
				:style="chatSurface ? paletteVars : null"
			>
				<main class="jv-sup-center">
					<slot />
				</main>
				<!-- Right details panel — thread only. Hidden on narrow viewports. -->
				<aside v-if="$slots.aside" class="jv-sup-aside">
					<slot name="aside" />
				</aside>
			</div>
		</div>
	</div>
</template>

<script setup>
// SupportShell is NOT a jv-root itself: the rail + bar are painted in frappe-ui
// tokens to match the app shell, and the jv palette is applied only around the
// chat-surface content (see chatSurface). Brand + Desk-open + chat navigation now
// live in SupportSidebar.
import { Breadcrumbs } from "frappe-ui";
import SupportSidebar from "@/components/support/SupportSidebar.vue";
import { useJarvisTheme } from "@/theme";

defineProps({
	// Breadcrumbs trail, e.g. [{label:'Support', route:{name:'Support'}}, {label:'#123'}].
	crumbs: { type: Array, default: () => [{ label: "Support" }] },
	// true on pages that host the chat Composer/Message + the details panel (thread,
	// new) — wraps the content row in a jv-root palette surface. Omitted on the list.
	chatSurface: { type: Boolean, default: false },
});

const { effectiveDark, paletteVars } = useJarvisTheme();
</script>

<style scoped>
.jv-sup {
	display: flex;
	flex-direction: row;
	height: 100%;
	width: 100%;
	overflow: hidden;
	background: var(--surface-white);
	color: var(--ink-gray-8);
	font-family: "Inter", system-ui, sans-serif;
}
.jv-sup-main {
	display: flex;
	flex-direction: column;
	flex: 1;
	min-width: 0;
	overflow: hidden;
}
.jv-sup-bar {
	display: flex;
	align-items: center;
	gap: 10px;
	flex: 0 0 auto;
	height: 52px;
	padding: 0 20px;
	border-bottom: 1px solid var(--outline-gray-1);
	background: var(--surface-white);
}
.jv-sup-right {
	margin-left: auto;
	display: flex;
	align-items: center;
	gap: 8px;
	flex: 0 0 auto;
}
.jv-sup-content {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: row;
	overflow: hidden;
}
/* Chat-surface content adopts the jv palette (same as the old jv-sup-chat). */
.jv-sup-content.jv-root {
	color: var(--text);
	background: var(--surface);
}
.jv-sup-center {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	overflow: hidden;
}
.jv-sup-aside {
	display: flex;
	flex: 0 0 auto;
	min-height: 0;
}
/* Narrow viewports: drop the details panel (the collapsed reply bar in the center
   is still the primary reply path); the rail stays and can be collapsed manually. */
@media (max-width: 900px) {
	.jv-sup-aside {
		display: none;
	}
}
</style>
