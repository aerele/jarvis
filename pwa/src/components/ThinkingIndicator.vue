<script setup>
import { onMounted, onUnmounted, ref } from "vue";

// A run can spend a long time in a tool before a single token streams back. A
// bare spinner reads as "stuck", so say what it is plausibly doing — same
// rotating copy as the native app.
const WORDS = [
	"Thinking…",
	"Working on it…",
	"Reading your records…",
	"Reasoning it through…",
	"Checking the details…",
	"Putting it together…",
	"Almost there…",
];

const props = defineProps({ label: { type: String, default: "" } });

const i = ref(0);
let timer = null;

onMounted(() => {
	timer = setInterval(() => {
		i.value = (i.value + 1) % WORDS.length;
	}, 2400);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
	<div class="jv-thinking">
		<span class="jv-dot" /><span class="jv-dot" /><span class="jv-dot" />
		<span class="jv-thinking-text">{{ props.label || WORDS[i] }}</span>
	</div>
</template>

<style scoped>
.jv-thinking {
	display: flex;
	align-items: center;
	gap: 4px;
	padding: 2px;
}
.jv-dot {
	width: 6px;
	height: 6px;
	border-radius: 50%;
	background: var(--accent);
	animation: jv-pulse 1.2s infinite ease-in-out;
}
.jv-dot:nth-child(2) {
	animation-delay: 0.17s;
}
.jv-dot:nth-child(3) {
	animation-delay: 0.34s;
}
.jv-thinking-text {
	margin-left: 5px;
	font-size: 13px;
	font-weight: 500;
	color: var(--ink5);
}
@keyframes jv-pulse {
	0%,
	60%,
	100% {
		opacity: 0.35;
		transform: scale(0.85);
	}
	30% {
		opacity: 1;
		transform: scale(1.15);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-dot {
		animation: none;
		opacity: 0.6;
	}
}
</style>
