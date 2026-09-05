<template>
	<!-- Always-on version status in the chat top bar. A real focusable button
	     whose aria-label matches its visible label; click opens What's-new. No
	     pill at all when the target version is unknown (never a false "current"). -->
	<button
		v-if="pill.show"
		ref="btnEl"
		type="button"
		class="jv-versionpill"
		:class="['jv-tone-' + pill.tone, { 'jv-pill-pulse': pulsing }]"
		:aria-label="pill.label"
		:title="title"
		@click="openWhatsNew"
		@animationend="pulsing = false"
	>
		<span class="jv-pill-dot" aria-hidden="true"></span>
		<span class="jv-pill-label">{{ pill.label }}</span>
	</button>
</template>

<script setup>
// VersionPill: the customer-facing "how current is my Jarvis" signal (Slice 3b).
// Hand-styled to mirror the header's .jv-modelpill (a coloured dot + label),
// tone driven by the app palette vars (--green/--amber/--red) rather than a
// frappe-ui Badge (there is no "amber" Badge theme). The label text is derived
// once from the stable boot payload (pillFor) - no per-render work.
import { ref } from "vue";
import { pillFor } from "@/releaseNudge";
import { notice, openWhatsNew } from "@/noticeGate";
import { agentName } from "@/branding";

defineProps({
	// Hover tooltip; the visible label already lives in the pill + aria-label.
	title: { type: String, default: "See what's new" },
});

const btnEl = ref(null);
const pulsing = ref(false);

// Boot payload is stable for the page's lifetime, so derive once.
const pill = pillFor(notice, agentName);

// The soft banner's minimise-into-pill animation calls this as the banner
// "arrives", for a small catch-pulse. Restart-safe on repeat dismisses; the
// CSS keyframe is disabled under prefers-reduced-motion (below).
function pulse() {
	pulsing.value = false;
	requestAnimationFrame(() => {
		pulsing.value = true;
	});
}

// The banner (a sibling) needs the pill's rect (FLIP target) and the pulse.
// getEl() returns the raw element (or null when the pill is hidden).
defineExpose({ getEl: () => btnEl.value, pulse });
</script>

<style scoped>
/* Mirrors .jv-modelpill in ChatView.vue: same rounded, bordered chip on the
   surface-1 fill, but with a leading tone dot instead of the action icon. */
.jv-versionpill {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 5px 10px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 20px;
	cursor: pointer;
	font-family: inherit;
	line-height: 1;
}
.jv-versionpill:hover {
	border-color: var(--border-2);
	background: var(--surface-2);
}
.jv-versionpill:focus-visible {
	outline: 2px solid var(--link);
	outline-offset: 2px;
}

.jv-pill-dot {
	width: 7px;
	height: 7px;
	border-radius: 50%;
	flex: none;
	background: var(--text-3);
}
.jv-pill-label {
	font-size: 12px;
	font-weight: 500;
	color: var(--text-2);
	white-space: nowrap;
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
