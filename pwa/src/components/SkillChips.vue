<script setup>
// "This answer used your Pricing skill." Small provenance chips, from the
// agent's ```jarvis-skill``` block.
const props = defineProps({ names: { type: Array, default: () => [] } });
</script>

<template>
	<div v-if="props.names.length" class="jv-skills">
		<span v-for="n in props.names" :key="n" class="jv-skill-chip">
			<svg
				viewBox="0 0 24 24"
				width="11"
				height="11"
				fill="none"
				stroke="currentColor"
				stroke-width="1.9"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M12 2 4 7v10l8 5 8-5V7z" />
				<path d="M12 22V12M12 12 4 7M12 12l8-5" />
			</svg>
			{{ n }}
		</span>
	</div>
</template>

<style scoped>
.jv-skills {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
	margin-top: 8px;
}
.jv-skill-chip {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	padding: 4px 9px;
	border-radius: 999px;
	background: var(--accent-bg);
	color: var(--accent);
	font-size: 11px;
	font-weight: 500;
}
</style>
