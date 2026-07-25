<template>
	<div class="jv-sup">
		<!-- Frappe-ui-styled top bar that mirrors the app's LayoutHeader (Skills /
		     Macros): a Breadcrumbs title on the left, then the standard
		     "Open ERPNext Desk" outline button + the page's actions on the right.
		     Uses frappe-ui semantic tokens (surface-white / ink-gray / outline-gray),
		     NOT the chat palette, so the standalone support pages look and feel like
		     any other page in the app. -->
		<header class="jv-sup-bar">
			<button
				class="jv-sup-home"
				aria-label="Back to Jarvis"
				title="Back to Jarvis"
				@click="goHome"
			>
				<JarvisMark :size="24" :radius="6" />
			</button>
			<Breadcrumbs class="min-w-0" :items="crumbs" />
			<div class="jv-sup-right">
				<Button
					variant="outline"
					size="sm"
					icon="external-link"
					label="Open ERPNext Desk"
					:tooltip="'Open ERPNext Desk'"
					class="jv-deskbtn"
					@click="openDesk"
				/>
				<slot name="actions" />
			</div>
		</header>

		<main class="jv-sup-body">
			<!-- Chat-surface pages (thread, new) reuse the chat Composer/Message, which
			     need the jv-root palette-vars + the index.css forms reset. Wrap ONLY
			     those. The list page renders bare so its reused frappe-ui ListPage
			     controls keep their stock look (the forms reset would otherwise strip
			     the search box down to a UA input). -->
			<div
				v-if="chatSurface"
				class="jv-root jv-sup-chat"
				:class="{ 'jv-dark': effectiveDark }"
				:style="paletteVars"
			>
				<slot />
			</div>
			<slot v-else />
		</main>
	</div>
</template>

<script setup>
// SupportShell - the standalone support chrome. It is NOT a jv-root itself: the
// bar is painted in frappe-ui tokens to match the app shell's header, and the
// chat palette is applied only around chat-surface page bodies (see chatSurface).
import { useRouter } from "vue-router";
import { Breadcrumbs, Button } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { useJarvisTheme } from "@/theme";

defineProps({
	// Breadcrumbs trail, e.g. [{label:'Support', route:{name:'Support'}}, {label:'#123'}].
	// The last item renders as the current page; earlier items are links.
	crumbs: { type: Array, default: () => [{ label: "Support" }] },
	// true on pages that host the chat Composer/Message (thread, new) — wraps the
	// body in a jv-root palette surface. Omitted on the frappe-ui list page.
	chatSurface: { type: Boolean, default: false },
});

const router = useRouter();
const { effectiveDark, paletteVars } = useJarvisTheme();

// The brand mark is the home affordance back to the Jarvis app (there is no
// sidebar on the standalone route). Desk opens in a new tab, exactly like the
// app header's Go-to-Desk button (LayoutHeader.openDesk).
function goHome() {
	router.push({ name: "Chat" });
}
function openDesk() {
	window.open("/app", "_blank");
}
</script>

<style scoped>
.jv-sup {
	display: flex;
	flex-direction: column;
	height: 100%;
	width: 100%;
	overflow: hidden;
	background: var(--surface-white);
	color: var(--ink-gray-8);
	font-family: "Inter", system-ui, sans-serif;
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
.jv-sup-home {
	display: flex;
	align-items: center;
	flex: 0 0 auto;
	padding: 2px;
	border: 0;
	border-radius: 6px;
	background: transparent;
	cursor: pointer;
}
.jv-sup-home:hover {
	opacity: 0.82;
}
.jv-sup-right {
	margin-left: auto;
	display: flex;
	align-items: center;
	gap: 8px;
	flex: 0 0 auto;
}
.jv-sup-body {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	overflow: hidden;
}
/* The list page passes its own `min-h-0 flex-1` to ListPage; the chat wrapper
   fills via .jv-sup-chat below. */
.jv-sup-chat {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	overflow: hidden;
	color: var(--text);
	background: var(--surface);
	font-family: "Inter", system-ui, sans-serif;
}
</style>
