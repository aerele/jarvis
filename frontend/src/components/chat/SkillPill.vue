<!--
  One thing picked in the composer's "+" menu (ComposerPlusMenu) for the next message:
  a skill (kind "skill"), wiki grounding ("wiki") or a connector focus ("connector");
  × drops it. The skill pill sits beside the + button on desktop; the PWA, whose composer
  row is too narrow, shows all three above its input. Shared through @shared.
-->
<template>
	<span class="jv-skillpill" :title="KINDS[kind].title(name)">
		<svg
			width="13"
			height="13"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="1.8"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path v-for="d in KINDS[kind].icon" :key="d" :d="d" />
		</svg>
		<span class="jv-skillpill-text">{{ name }}</span>
		<button
			type="button"
			class="jv-skillpill-x"
			:title="KINDS[kind].clear"
			:aria-label="KINDS[kind].clear"
			:disabled="disabled"
			@click="emit('clear')"
		>
			×
		</button>
	</span>
</template>

<script setup>
defineProps({
	name: { type: String, required: true },
	kind: { type: String, default: "skill" },
	disabled: { type: Boolean, default: false },
});
const KINDS = {
	skill: {
		icon: ["M13 2 3 14h9l-1 8 10-12h-9l1-8Z"],
		title: (n) => `Using the skill ${n}`,
		clear: "Don't use this skill",
	},
	wiki: {
		icon: [
			"M4 19.5A2.5 2.5 0 0 1 6.5 17H20",
			"M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z",
		],
		title: () => "The next answer is grounded in your wiki",
		clear: "Don't ground in the wiki",
	},
	connector: {
		icon: ["M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"],
		title: (n) => `Focused on ${n}`,
		clear: "Clear focus",
	},
};
const emit = defineEmits(["clear"]);
</script>

<style scoped>
.jv-skillpill {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	height: 26px;
	max-width: 200px;
	padding: 0 2px 0 8px;
	border: 1px solid var(--cta, var(--accent));
	border-radius: 7px;
	color: var(--cta, var(--accent));
	font-size: 12.5px;
	font-weight: 500;
	min-width: 0;
}
.jv-skillpill-text {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-skillpill-x {
	width: 20px;
	height: 20px;
	border: none;
	background: transparent;
	color: inherit;
	border-radius: 5px;
	cursor: pointer;
	font-size: 14px;
	line-height: 1;
	flex: none;
}
.jv-skillpill-x:hover:not(:disabled) {
	background: var(--surface-2, var(--card2));
}
</style>
