<script setup>
// UpdateBanner: the occasional soft nudge that a newer app version exists
// (Slice 3b), mirroring the desktop SPA's chat/UpdateBanner.vue in the PWA's
// own idiom. Mounted at the app shell (App.vue), right next to InstallBanner -
// NOT inside ChatView - because in the PWA the version pill is chat-only but
// this nudge is app-wide (see noticeGate.js's pillHandle for how the two find
// each other across that split).
//
// Dismiss ("Remind me later" or x) plays a short minimise-into-pill FLIP
// toward whichever VersionPill instance is currently registered (null when the
// user isn't on the Chat route, or before ChatView has mounted one - in which
// case this degrades to a plain hide), then snoozes per-device. Honors
// prefers-reduced-motion (instant hide + snooze, no FLIP/pulse). Visibility
// (yield to InstallBanner, and to a signed-out visitor) is decided by the
// caller's v-if in App.vue.
import { ref } from "vue";
import { agentName } from "@/branding";
import { pillHandle, snoozeBanner, openWhatsNew } from "../noticeGate";

const message = `A new version of ${agentName} is available — ask your administrator to update.`;

const bannerEl = ref(null);
const flipStyle = ref({});

function reducedMotion() {
	try {
		return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
	} catch (e) {
		return false;
	}
}

function dismiss() {
	const el = bannerEl.value;
	const pill = pillHandle.current;
	const pillEl = pill && pill.getEl ? pill.getEl() : null;

	// Reduced motion, or no banner element: instant hide + snooze.
	if (reducedMotion() || !el) {
		snoozeBanner();
		return;
	}

	const from = el.getBoundingClientRect();
	const to = pillEl && pillEl.getBoundingClientRect ? pillEl.getBoundingClientRect() : null;

	// No pill registered right now (not on the Chat route), or its rect is
	// unmeasurable (hidden/offscreen): degrade to a plain hide + snooze.
	if (!to || !to.width || !to.height) {
		snoozeBanner();
		return;
	}

	const dx = to.left + to.width / 2 - (from.left + from.width / 2);
	const dy = to.top + to.height / 2 - (from.top + from.height / 2);

	// Small catch-pulse on the pill as the banner arrives (~end of the hop).
	setTimeout(() => {
		if (pill && pill.pulse) pill.pulse();
	}, 380);

	flipStyle.value = {
		transition: "transform 0.5s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.5s ease",
		transformOrigin: "center",
		willChange: "transform, opacity",
	};
	requestAnimationFrame(() => {
		flipStyle.value = {
			...flipStyle.value,
			transform: `translate(${dx}px, ${dy}px) scale(0.05)`,
			opacity: 0,
			pointerEvents: "none",
		};
	});

	// Snooze (which unmounts us via the reactive showBanner) once the hop ends.
	// transitionend fires per-property; `done` guards the double fire, and the
	// timeout is a safety net if transitionend never arrives.
	let done = false;
	const finish = () => {
		if (done) return;
		done = true;
		snoozeBanner();
	};
	el.addEventListener("transitionend", finish, { once: true });
	setTimeout(finish, 650);
}
</script>

<template>
	<!-- Calm info/blue treatment - NOT the alarm amber the hard gate implies.
	     The wrapper is the FLIP target that minimises into the version pill on
	     dismiss. -->
	<div ref="bannerEl" class="jv-updatebanner" :style="flipStyle">
		<svg
			class="jv-ub-icon"
			viewBox="0 0 24 24"
			width="16"
			height="16"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
		>
			<circle cx="12" cy="12" r="9" />
			<path d="M12 8v5M12 16h.01" />
		</svg>
		<div class="jv-ub-text">{{ message }}</div>
		<div class="jv-ub-actions">
			<button class="jv-ub-btn" type="button" @click="openWhatsNew">What's new</button>
			<button class="jv-ub-btn jv-ub-btn--ghost" type="button" @click="dismiss">
				Remind me later
			</button>
			<button
				class="jv-ub-x"
				type="button"
				aria-label="Dismiss update banner"
				@click="dismiss"
			>
				<svg
					viewBox="0 0 24 24"
					width="14"
					height="14"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
				>
					<path d="M18 6 6 18M6 6l12 12" />
				</svg>
			</button>
		</div>
	</div>
</template>

<style scoped>
.jv-updatebanner {
	flex: none;
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 10px;
	margin: 8px 12px 0;
	padding: 12px;
	border-radius: 14px;
	background: var(--blue-bg);
	border: 1px solid color-mix(in srgb, var(--blue) 35%, transparent);
	color: var(--blue);
}
.jv-ub-icon {
	flex: none;
}
.jv-ub-text {
	flex: 1;
	min-width: 160px;
	font-size: 13px;
	line-height: 1.4;
	color: var(--ink7);
}
.jv-ub-actions {
	display: flex;
	align-items: center;
	gap: 6px;
	flex: none;
}
.jv-ub-btn {
	height: 30px;
	padding: 0 11px;
	border: 1px solid color-mix(in srgb, var(--blue) 40%, transparent);
	border-radius: 8px;
	background: transparent;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--blue);
	cursor: pointer;
	white-space: nowrap;
}
.jv-ub-btn--ghost {
	border-color: transparent;
	color: var(--ink6);
}
.jv-ub-btn:active {
	background: color-mix(in srgb, var(--blue) 16%, transparent);
}
.jv-ub-btn:focus-visible,
.jv-ub-x:focus-visible {
	outline: 2px solid var(--blue);
	outline-offset: 2px;
}
.jv-ub-x {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 30px;
	height: 30px;
	flex: none;
	border: none;
	border-radius: 8px;
	background: transparent;
	color: var(--ink5);
	cursor: pointer;
}
.jv-ub-x:active {
	background: color-mix(in srgb, var(--ink9) 10%, transparent);
}
</style>
