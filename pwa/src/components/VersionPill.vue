<script setup>
// VersionPill: the customer-facing "how current is my Jarvis" signal
// (Slice 3b), mirroring the desktop SPA's chat/VersionPill.vue in the PWA's
// own hand-styled .jv- idiom. Mounted in ChatView's tight .jv-bar header row,
// so it stays compact: a coloured dot + a label that truncates rather than
// wrapping. The label is derived once from the stable boot payload (pillFor) -
// no per-render work. No pill at all when the target version is unknown
// (never a false "current").
import { onBeforeUnmount, onMounted, ref } from "vue";
import { pillFor } from "@shared/releaseNudge";
import { notice, openWhatsNew, pillHandle } from "../noticeGate";
import { agentName } from "@/branding";

const btnEl = ref(null);
const pulsing = ref(false);

// Boot payload is stable for the page's lifetime, so derive once.
const pill = pillFor(notice, agentName);
// The screen-reader label names the action ("… — see what's new"); the visible
// text stays the terse status label. Only meaningful when the pill renders.
const pillAriaLabel = pill.show ? `${pill.label} — see what's new` : "";

// The soft banner's minimise-into-pill animation calls this as the banner
// "arrives", for a small catch-pulse. Restart-safe on repeat dismisses; the
// CSS keyframe is disabled under prefers-reduced-motion (below).
function pulse() {
	pulsing.value = false;
	requestAnimationFrame(() => {
		pulsing.value = true;
	});
}

// The app-shell UpdateBanner (a different template entirely - see
// noticeGate.js) needs this pill's rect (FLIP target) and its pulse(). Register
// on mount, clear on unmount, so a banner shown outside the Chat route finds no
// target and degrades to a plain hide.
const handle = { getEl: () => btnEl.value, pulse };
onMounted(() => {
	pillHandle.current = handle;
});
onBeforeUnmount(() => {
	if (pillHandle.current === handle) pillHandle.current = null;
});

defineExpose({ getEl: handle.getEl, pulse });
</script>

<template>
	<button
		v-if="pill.show"
		ref="btnEl"
		type="button"
		class="jv-versionpill"
		:class="['jv-tone-' + pill.tone, { 'jv-pill-pulse': pulsing }]"
		:aria-label="pillAriaLabel"
		:title="pill.label"
		@click="openWhatsNew"
		@animationend="pulsing = false"
	>
		<span class="jv-pill-dot" aria-hidden="true"></span>
		<span class="jv-pill-label">{{ pill.label }}</span>
	</button>
</template>

<style scoped>
.jv-versionpill {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	max-width: 112px;
	flex: none;
	padding: 5px 9px;
	background: var(--card2);
	border: 1px solid var(--border2);
	border-radius: 20px;
	cursor: pointer;
	font-family: inherit;
	line-height: 1;
}
.jv-versionpill:active {
	background: var(--card3);
}
.jv-versionpill:focus-visible {
	outline: 2px solid var(--accent);
	outline-offset: 2px;
}

.jv-pill-dot {
	width: 7px;
	height: 7px;
	border-radius: 50%;
	flex: none;
	background: var(--ink5);
}
.jv-pill-label {
	font-size: 11px;
	font-weight: 500;
	color: var(--ink6);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}

/* Tone drives the dot AND the label colour, straight off the app palette. */
.jv-tone-green .jv-pill-dot {
	background: var(--green);
}
.jv-tone-green .jv-pill-label {
	color: var(--green);
}
.jv-tone-amber .jv-pill-dot {
	background: var(--amber);
}
.jv-tone-amber .jv-pill-label {
	color: var(--amber);
}
.jv-tone-red .jv-pill-dot {
	background: var(--red);
}
.jv-tone-red .jv-pill-label {
	color: var(--red);
}

/* Catch-pulse when the soft banner minimises into the pill. */
@keyframes jvPillPulse {
	0% {
		transform: scale(1);
	}
	45% {
		transform: scale(1.16);
	}
	100% {
		transform: scale(1);
	}
}
.jv-pill-pulse {
	animation: jvPillPulse 0.45s ease;
}
@media (prefers-reduced-motion: reduce) {
	.jv-pill-pulse {
		animation: none;
	}
}
</style>
