<template>
	<button
		v-if="context && context.fresh"
		type="button"
		data-testid="context-pill"
		class="jv-ctx"
		:class="{ 'jv-ctx-warn': warn && !compacting && !compacted, 'jv-ctx-busy': compacting }"
		:title="title"
		:aria-label="title"
		:disabled="compacting"
		@click="$emit('compact')"
	>
		<span class="jv-ctx-bar" aria-hidden="true">
			<i class="jv-ctx-fill" :style="{ width: fillPct + '%' }"></i>
			<b
				v-if="context.auto_compact_pct > 0"
				data-testid="auto-tick"
				class="jv-ctx-tick"
				:style="{ left: context.auto_compact_pct + '%' }"
			></b>
		</span>
		<span class="jv-ctx-label">{{ label }}</span>
	</button>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
	context: { type: Object, default: null },
	compacting: { type: Boolean, default: false },
	compacted: { type: Boolean, default: false },
});
defineEmits(["compact"]);

function k(n) {
	n = Number(n) || 0;
	return n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}
const warn = computed(() => props.context && props.context.pct >= props.context.warn_pct);
const fillPct = computed(() => {
	if (props.compacted) return 0;
	if (props.compacting) return 100;
	return Math.min(100, Math.max(0, props.context?.pct || 0));
});
const label = computed(() => {
	if (props.compacting) return "Compacting…";
	if (props.compacted) return "Compacted";
	return `${k(props.context.used)} / ${k(props.context.capacity)}`;
});
const title = computed(() =>
	props.compacting
		? "Compacting this chat"
		: props.compacted
		? "Context compacted. The meter updates after your next message."
		: "Context in use. Compacts automatically near the tick. Click to compact now."
);
</script>

<style scoped>
.jv-ctx {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	height: 26px;
	padding: 0 8px;
	border-radius: 6px;
	border: 1px solid var(--border);
	background: var(--surface-2);
	color: var(--text-3);
	font: inherit;
	font-size: 11.5px;
	font-variant-numeric: tabular-nums;
	white-space: nowrap;
	cursor: pointer;
}
.jv-ctx:hover {
	background: var(--surface-3);
}
.jv-ctx:focus-visible {
	outline: 2px solid var(--brand-1);
	outline-offset: 1px;
}
.jv-ctx-warn {
	color: #d97706;
	border-color: #d97706;
	background: rgba(217, 119, 6, 0.08);
}
.jv-ctx-warn .jv-ctx-fill {
	background: #d97706;
}
.jv-ctx-bar {
	position: relative;
	width: 48px;
	height: 5px;
	border-radius: 99px;
	background: var(--surface-3);
	overflow: hidden;
}
.jv-ctx-fill {
	display: block;
	height: 100%;
	border-radius: 99px;
	background: var(--text-3);
	transition: width 0.6s ease;
}
.jv-ctx-tick {
	position: absolute;
	top: -2px;
	bottom: -2px;
	width: 1px;
	background: #d97706;
	opacity: 0.7;
}
.jv-ctx-busy {
	color: var(--text-2);
	border-color: transparent;
	background: var(--surface-3);
	position: relative;
	overflow: hidden;
	cursor: default;
}
.jv-ctx-busy .jv-ctx-fill {
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
	animation: jv-ctx-drain 3s ease-in-out infinite;
}
.jv-ctx-busy::after {
	content: "";
	position: absolute;
	inset: 0;
	background: linear-gradient(
		100deg,
		transparent 30%,
		rgba(255, 255, 255, 0.35) 50%,
		transparent 70%
	);
	animation: jv-ctx-sheen 1.4s linear infinite;
}
@keyframes jv-ctx-drain {
	0%,
	20% {
		width: 84%;
	}
	60%,
	100% {
		width: 30%;
	}
}
@keyframes jv-ctx-sheen {
	from {
		transform: translateX(-100%);
	}
	to {
		transform: translateX(100%);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-ctx-busy .jv-ctx-fill,
	.jv-ctx-busy::after {
		animation: none;
	}
}
</style>
