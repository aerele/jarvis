<script setup>
// The agent's ```jarvis-cards``` payload: a list of ERP records with a few
// fields each. Rendered as cards instead of a markdown table — a table with six
// columns is unreadable on a phone, and the raw JSON (what the PWA showed
// before) is unreadable anywhere.
const props = defineProps({ data: { type: Object, required: true } });
</script>

<template>
	<div class="jv-cards">
		<div v-if="props.data.title" class="jv-cards-title">{{ props.data.title }}</div>
		<div v-for="(c, i) in props.data.cards" :key="i" class="jv-rcard">
			<div v-if="c.title" class="jv-rcard-title">{{ c.title }}</div>
			<div v-if="c.subtitle" class="jv-rcard-sub">{{ c.subtitle }}</div>
			<div v-for="(f, fi) in c.fields" :key="fi" class="jv-rcard-field">
				<span class="jv-rcard-label">{{ f.label }}</span>
				<span class="jv-rcard-value">{{ f.value }}</span>
			</div>
		</div>
	</div>
</template>

<style scoped>
.jv-cards {
	display: flex;
	flex-direction: column;
	gap: 8px;
	margin-top: 8px;
}
.jv-cards-title {
	font-size: 11px;
	font-weight: 600;
	letter-spacing: 0.4px;
	text-transform: uppercase;
	color: var(--ink5);
}
.jv-rcard {
	padding: 12px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
}
.jv-rcard-title {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-rcard-sub {
	margin-top: 1px;
	font-size: 12px;
	color: var(--ink5);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-rcard-field {
	display: flex;
	justify-content: space-between;
	gap: 12px;
	margin-top: 6px;
	font-size: 12px;
}
.jv-rcard-label {
	color: var(--ink5);
	flex: none;
}
.jv-rcard-value {
	font-weight: 500;
	color: var(--ink8);
	text-align: right;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
</style>
