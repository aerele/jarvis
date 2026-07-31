<template>
	<div ref="rootRef" class="pep">
		<!-- trigger pill: sits in the composer toolbar, next to the model pill -->
		<button
			ref="triggerRef"
			class="pep-pill"
			type="button"
			:aria-expanded="open"
			:title="`Persona: ${persona}`"
			@click="open = !open"
		>
			<span class="pep-orb sm" :class="persona.toLowerCase()" aria-hidden="true">
				<JarvisMark v-if="persona === 'Jarvis'" :size="18" :radius="9" />
				<svg v-else viewBox="0 0 24 24" fill="#fff">
					<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
				</svg>
			</span>
			<span class="pep-name">{{ persona }}</span>
			<svg
				class="pep-caret"
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.9"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		<!-- dropdown, opening UPWARD (matches the model picker) -->
		<div v-if="open" class="pep-menu" role="menu">
			<button
				v-for="opt in options"
				:key="opt.value"
				class="pep-item"
				role="menuitemradio"
				:aria-checked="persona === opt.value"
				@click="pick(opt.value)"
			>
				<span class="pep-orb" :class="opt.value.toLowerCase()" aria-hidden="true">
					<JarvisMark v-if="opt.value === 'Jarvis'" :size="24" :radius="12" />
					<svg v-else viewBox="0 0 24 24" fill="#fff">
						<path
							d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
						/>
					</svg>
				</span>
				<span class="pep-body">
					<span class="pep-item-name">{{ opt.name }}</span>
					<span class="pep-desc">{{ opt.desc }}</span>
				</span>
				<CheckMark v-if="persona === opt.value" />
			</button>
			<div class="pep-note">
				Persona is voice only. It applies to every chat and device, and takes effect from
				the next message.
			</div>
		</div>
	</div>
</template>

<script setup>
// Persona picker for the composer — same interaction as the model pill
// (ModelEffortPicker): a pill in the input toolbar opening a compact upward
// dropdown of the two personas, each with its living mark. Tone only, never
// tools or permissions. Reads/writes the shell store (roams to the server).
import { computed, ref } from "vue";
import { useShellStore } from "@/stores/shell";
import { useDismissable } from "@/composables/useDismissable";
import CheckMark from "@/components/chat/CheckMark.vue";
import JarvisMark from "@/components/JarvisMark.vue";

const store = useShellStore();
const open = ref(false);
const rootRef = ref(null);
const triggerRef = ref(null);

const persona = computed(() => store.preferredPersona);
const options = [
	{ value: "Jarvis", name: "Jarvis", desc: "steady · direct" },
	{ value: "Jara", name: "Jara", desc: "calm · warm" },
];

function pick(v) {
	store.setPreferredPersona(v);
	open.value = false;
	triggerRef.value?.focus();
}
useDismissable(rootRef, open, undefined, triggerRef);
</script>

<style scoped>
.pep {
	position: relative;
	display: inline-flex;
}

/* ---- trigger pill ---- */
.pep-pill {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	height: 30px;
	padding: 0 11px 0 8px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 999px;
	cursor: pointer;
	color: var(--text-2);
	font-family: inherit;
	font-size: 12px;
	font-weight: 500;
	transition: background-color 0.12s, border-color 0.12s;
}
.pep-pill:hover {
	background: var(--surface-2);
}
.pep-pill:focus-visible {
	outline: 2px solid var(--text);
	outline-offset: 2px;
}
.pep-name {
	color: var(--text-2);
}
.pep-caret {
	color: var(--text-3);
}

/* ---- the living mark ---- */
.pep-orb {
	position: relative;
	display: grid;
	place-items: center;
	width: 24px;
	height: 24px;
	flex: none;
	border-radius: 50%;
}
.pep-orb.sm {
	width: 18px;
	height: 18px;
}
.pep-orb svg {
	width: 55%;
	height: 55%;
	filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.22));
}
.pep-orb.jarvis {
	background: radial-gradient(
			circle at 34% 30%,
			rgba(255, 255, 255, 0.55),
			rgba(255, 255, 255, 0) 60%
		),
		var(--brand-grad, linear-gradient(140deg, #6e8bff, #8b5cf6));
	box-shadow: 0 0 10px rgba(124, 116, 246, 0.4);
}
.pep-orb.jara {
	background: radial-gradient(
			circle at 34% 30%,
			rgba(255, 255, 255, 0.55),
			rgba(255, 255, 255, 0) 60%
		),
		linear-gradient(140deg, #9d7cea, #6846e3);
	box-shadow: 0 0 10px rgba(124, 92, 234, 0.4);
}
/* The Jarvis orb renders <JarvisMark>, whose default .jv-mark paints its own
   opaque brand gradient at the full orb size - which covered .pep-orb.jarvis's
   radial highlight, so the Jarvis orb read flat while Jara's white star (a bare
   SVG over the same orb) glowed. Drop the nested mark's own background so the
   orb's highlight shows through behind the star, matching Jara. A tenant
   whitelabel logo (.jv-mark-img) is exempt - an image must stay opaque. */
.pep-orb.jarvis :deep(.jv-mark:not(.jv-mark-img)) {
	background: transparent;
}

/* ---- dropdown ---- */
.pep-menu {
	position: absolute;
	bottom: calc(100% + 8px);
	left: 0;
	min-width: 236px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 12px;
	box-shadow: 0 12px 34px rgba(20, 20, 30, 0.18);
	padding: 5px;
	z-index: 60;
}
.pep-item {
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
.pep-item:hover {
	background: var(--surface-1);
}
.pep-body {
	display: flex;
	flex-direction: column;
	gap: 1px;
	flex: 1;
	min-width: 0;
}
.pep-item-name {
	font-size: 13px;
	font-weight: 500;
	color: var(--text);
}
.pep-desc {
	font-size: 11.5px;
	font-weight: 450;
	color: var(--text-3);
	text-transform: capitalize;
}
.pep-note {
	padding: 8px 9px 4px;
	font-size: 11.5px;
	font-weight: 450;
	line-height: 1.4;
	color: var(--text-3);
}

/* a touch of life: the dropdown marks breathe gently (motion-safe) */
@media (prefers-reduced-motion: no-preference) {
	.pep-menu .pep-orb.jarvis {
		animation: pep-pulse 2.6s ease-in-out infinite;
	}
	.pep-menu .pep-orb.jara {
		animation: pep-drift 4.4s ease-in-out infinite;
	}
}
@keyframes pep-pulse {
	0%,
	100% {
		transform: scale(1);
	}
	50% {
		transform: scale(1.06);
	}
}
@keyframes pep-drift {
	0%,
	100% {
		transform: translateY(0) scale(1.01, 0.99);
	}
	50% {
		transform: translateY(-1.5px) scale(0.99, 1.01);
	}
}
</style>
