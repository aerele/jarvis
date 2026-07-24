<template>
	<div class="jv-root jv-support" :class="{ 'jv-dark': effectiveDark }" :style="paletteVars">
		<header class="jv-sup-bar">
			<button class="jv-sup-back" :aria-label="backLabel" @click="goBack">
				<JarvisMark :size="24" :radius="6" />
				<span class="jv-sup-backtext">{{ backLabel }}</span>
			</button>
			<div class="jv-sup-title" :title="title">{{ title }}</div>
			<div class="jv-sup-actions"><slot name="actions" /></div>
		</header>
		<main class="jv-sup-body"><slot /></main>
	</div>
</template>

<script setup>
// ALL THREE of jv-root / jv-dark / paletteVars on the template's root div are
// load-bearing; see the plan's Global Constraint 2. jv-root alone carries
// color-scheme, the ::placeholder color and the forms reset Composer's
// textarea depends on, and none of those failures are visible in a
// light-theme glance.
//
// That comment deliberately lives HERE and not as a `<!--  -->` directly
// above the div in the template: a sibling comment at the template's root
// level compiles to a two-node Fragment (comment + div), and
// @vue/test-utils then resolves `wrapper.element` to the outer mount
// container instead of this div — every classList/style assertion in
// support-shell.test.js would silently read the wrong node
// (VueWrapper#element: `hasMultipleRoots ? parentElement : vm.$el`).
import { useRouter } from "vue-router";
import JarvisMark from "@/components/JarvisMark.vue";
import { useJarvisTheme } from "@/theme";

const props = defineProps({
	title: { type: String, default: "Support" },
	// Where "Back to Jarvis" goes. Chat by default; the thread page overrides it
	// to the ticket list so Back walks the hierarchy rather than exiting support.
	backTo: { type: Object, default: () => ({ name: "Chat" }) },
	// Label paired with backTo — the list page keeps the default (exits support
	// entirely), while the thread and new-ticket pages pass "All tickets" since
	// they walk back UP the support hierarchy, not out of it. Drives both the
	// visible text and the aria-label so mobile (which hides jv-sup-backtext)
	// still announces the right destination.
	backLabel: { type: String, default: "Back to Jarvis" },
});

const router = useRouter();
const { effectiveDark, paletteVars } = useJarvisTheme();

function goBack() {
	router.push(props.backTo);
}
</script>

<style scoped>
.jv-support {
	display: flex;
	flex-direction: column;
	height: 100%;
	width: 100%;
	overflow: hidden;
	color: var(--text);
	background: var(--surface);
	font-family: "Inter", system-ui, sans-serif;
}
.jv-sup-bar {
	display: flex;
	align-items: center;
	gap: 12px;
	flex: 0 0 auto;
	height: 52px;
	padding: 0 16px;
	border-bottom: 1px solid var(--border);
	background: var(--surface-1);
}
.jv-sup-back {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 6px 10px 6px 6px;
	border: 0;
	border-radius: 8px;
	background: transparent;
	color: var(--text-2);
	cursor: pointer;
	font: inherit;
}
.jv-sup-back:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-sup-backtext {
	font-size: 13px;
}
.jv-sup-title {
	flex: 1;
	min-width: 0;
	font-size: 15px;
	font-weight: 600;
	color: var(--text);
	text-align: center;
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-sup-actions {
	display: flex;
	align-items: center;
	gap: 8px;
}
.jv-sup-body {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	overflow: hidden;
}
@media (max-width: 640px) {
	.jv-sup-backtext {
		display: none;
	}
	.jv-sup-title {
		text-align: left;
	}
}
</style>
