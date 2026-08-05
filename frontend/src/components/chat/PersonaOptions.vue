<template>
	<div class="pop" role="menu">
		<button
			v-for="opt in options"
			:key="opt.value"
			class="pop-item"
			role="menuitemradio"
			:aria-checked="persona === opt.value"
			@click="pick(opt.value)"
		>
			<span class="pop-orb" :class="opt.value.toLowerCase()" aria-hidden="true">
				<JarvisMark v-if="opt.value === 'Jarvis'" :size="24" :radius="12" />
				<svg v-else viewBox="0 0 24 24" fill="#fff">
					<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
				</svg>
			</span>
			<span class="pop-body">
				<span class="pop-name">{{ opt.name }}</span>
				<span class="pop-desc">{{ opt.desc }}</span>
			</span>
			<CheckMark v-if="persona === opt.value" />
		</button>
		<div class="pop-note">
			Persona is voice only. It applies to every chat and device, and takes effect from the
			next message.
		</div>
	</div>
</template>

<script setup>
// The persona option list (Jarvis / Jara), lifted out of the old standalone
// PersonaPill so the model picker's Persona flyout renders the exact same
// design. Store-backed: the choice roams to the server, same as before. The
// host owns the panel chrome and closing (via the `pick` emit).
import { computed } from "vue";
import { useShellStore } from "@/stores/shell";
import CheckMark from "@/components/chat/CheckMark.vue";
import JarvisMark from "@/components/JarvisMark.vue";

const emit = defineEmits(["pick"]);
const store = useShellStore();

const persona = computed(() => store.preferredPersona);
const options = [
	{ value: "Jarvis", name: "Jarvis", desc: "steady · direct" },
	{ value: "Jara", name: "Jara", desc: "calm · warm" },
];

function pick(v) {
	store.setPreferredPersona(v);
	emit("pick", v);
}
</script>

<style scoped>
.pop {
	display: flex;
	flex-direction: column;
}
.pop-item {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	padding: 8px 9px;
	border: none;
	border-radius: 8px;
	background: transparent;
	color: var(--text);
	font-family: inherit;
	text-align: left;
	cursor: pointer;
}
.pop-item:hover {
	background: var(--surface-1);
}
.pop-body {
	display: flex;
	flex-direction: column;
	gap: 1px;
	flex: 1;
	min-width: 0;
}
.pop-name {
	font-size: 13px;
	font-weight: 500;
	color: var(--text);
}
.pop-desc {
	font-size: 11.5px;
	font-weight: 450;
	color: var(--text-3);
	text-transform: capitalize;
}
.pop-note {
	padding: 8px 9px 4px;
	font-size: 11.5px;
	font-weight: 450;
	line-height: 1.4;
	color: var(--text-3);
}

/* ---- the living mark ---- */
.pop-orb {
	position: relative;
	display: grid;
	place-items: center;
	width: 24px;
	height: 24px;
	flex: none;
	border-radius: 50%;
}
.pop-orb svg {
	width: 55%;
	height: 55%;
	filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.22));
}
.pop-orb.jarvis {
	background: radial-gradient(
			circle at 34% 30%,
			rgba(255, 255, 255, 0.55),
			rgba(255, 255, 255, 0) 60%
		),
		var(--brand-grad, linear-gradient(140deg, #6e8bff, #8b5cf6));
	box-shadow: 0 0 10px rgba(124, 116, 246, 0.4);
}
.pop-orb.jara {
	background: radial-gradient(
			circle at 34% 30%,
			rgba(255, 255, 255, 0.55),
			rgba(255, 255, 255, 0) 60%
		),
		linear-gradient(140deg, #9d7cea, #6846e3);
	box-shadow: 0 0 10px rgba(124, 92, 234, 0.4);
}
/* Drop the nested JarvisMark's own opaque background so the orb's radial
   highlight shows through behind the star, matching Jara. A tenant whitelabel
   logo (.jv-mark-img) is exempt - an image must stay opaque. */
.pop-orb.jarvis :deep(.jv-mark:not(.jv-mark-img)) {
	background: transparent;
}

/* a touch of life: the marks breathe gently (motion-safe) */
@media (prefers-reduced-motion: no-preference) {
	.pop-orb.jarvis {
		animation: pop-pulse 2.6s ease-in-out infinite;
	}
	.pop-orb.jara {
		animation: pop-drift 4.4s ease-in-out infinite;
	}
}
@keyframes pop-pulse {
	0%,
	100% {
		transform: scale(1);
	}
	50% {
		transform: scale(1.06);
	}
}
@keyframes pop-drift {
	0%,
	100% {
		transform: translateY(0) scale(1.01, 0.99);
	}
	50% {
		transform: translateY(-1.5px) scale(0.99, 1.01);
	}
}
</style>
