<script setup>
// The agent has parked a write and is waiting for a human. This card is the
// only thing standing between the model and your ERP data, so it is loud on
// purpose: amber, full width, and it does not go away on its own.
const props = defineProps({ summary: { type: String, required: true } });
const emit = defineEmits(["open"]);
</script>

<template>
	<button class="jv-decision" @click="emit('open')">
		<span class="jv-decision-icon">
			<svg
				viewBox="0 0 24 24"
				width="17"
				height="17"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path
					d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
				/>
				<path d="M12 9v4M12 17h.01" />
			</svg>
		</span>
		<span class="jv-decision-main">
			<span class="jv-decision-title">{{ props.summary }}</span>
			<span class="jv-decision-sub">Review and approve</span>
		</span>
		<svg
			class="jv-decision-chev"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
		>
			<path d="m9 18 6-6-6-6" />
		</svg>
	</button>
</template>

<style scoped>
.jv-decision {
	display: flex;
	align-items: center;
	gap: 11px;
	width: 100%;
	padding: 13px;
	border: 1px solid var(--amber);
	border-radius: 12px;
	background: var(--amber-bg);
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-decision-icon {
	display: grid;
	place-items: center;
	width: 34px;
	height: 34px;
	flex: none;
	border-radius: 9px;
	background: var(--amber);
	color: #fff;
}
.jv-decision-main {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-decision-title {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
	line-height: 1.35;
}
.jv-decision-sub {
	margin-top: 1px;
	font-size: 11.5px;
	color: var(--amber);
}
.jv-decision-chev {
	width: 18px;
	height: 18px;
	flex: none;
	color: var(--amber);
}
</style>
