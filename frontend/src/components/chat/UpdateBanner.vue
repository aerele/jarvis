<template>
	<!-- Soft update banner: top-of-chat, calm info/blue (NOT the amber warning
	     the billing/readiness alerts use). The wrapper is the FLIP target that
	     minimises into the version pill on dismiss. -->
	<div ref="bannerEl" class="jv-updatebanner" :style="flipStyle">
		<Banner type="info" :message="message">
			<template #action>
				<button class="jv-ub-btn" type="button" @click="onWhatsNew">What's new</button>
				<button class="jv-ub-btn jv-ub-btn--ghost" type="button" @click="dismiss">
					Remind me later
				</button>
				<button
					class="jv-ub-x"
					type="button"
					aria-label="Dismiss update banner"
					@click="dismiss"
				>
					<FeatherIcon name="x" class="size-3.5" />
				</button>
			</template>
		</Banner>
	</div>
</template>

<script setup>
// UpdateBanner: the occasional soft nudge that a newer app version exists
// (Slice 3b). Dismiss ("Remind me later" or ×) plays a short minimise-into-pill
// FLIP, then snoozes per-device. Honors prefers-reduced-motion (instant hide +
// snooze, no FLIP/pulse), and degrades to a plain hide when the pill rect can't
// be measured. Mounting/visibility (yield to greeting/welcome/booting/urgent
// alerts) is decided by the caller's v-if in ChatView.
import { ref } from "vue";
import Banner from "@/components/Banner.vue";
import { FeatherIcon } from "frappe-ui";
import { agentName } from "@/branding";
import { snoozeBanner, openWhatsNew } from "@/noticeGate";

const props = defineProps({
	// The VersionPill's exposed instance ({ getEl, pulse }); may be null if the
	// pill isn't mounted/shown, in which case we skip the FLIP.
	pill: { type: Object, default: null },
});

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

function onWhatsNew() {
	openWhatsNew();
}

function dismiss() {
	const el = bannerEl.value;
	const pillEl = props.pill && props.pill.getEl ? props.pill.getEl() : null;

	// Reduced motion, or no banner element: instant hide + snooze.
	if (reducedMotion() || !el) {
		snoozeBanner();
		return;
	}

	const from = el.getBoundingClientRect();
	const to = pillEl && pillEl.getBoundingClientRect ? pillEl.getBoundingClientRect() : null;

	// Pill rect unmeasurable (hidden/offscreen): degrade to a plain hide + snooze.
	if (!to || !to.width || !to.height) {
		snoozeBanner();
		return;
	}

	const dx = to.left + to.width / 2 - (from.left + from.width / 2);
	const dy = to.top + to.height / 2 - (from.top + from.height / 2);

	// Small catch-pulse on the pill as the banner arrives (~end of the hop).
	setTimeout(() => {
		if (props.pill && props.pill.pulse) props.pill.pulse();
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

<style scoped>
.jv-updatebanner {
	margin: 12px 18px 0;
}

/* Compact, quiet action buttons inside the banner's #action slot. Real
   <button>s (keyboard-reachable); styled here since Banner is a child scope.
   Colours come from the app palette (theme-aware, resolved on .jv-root),
   --link being the one sanctioned blue. */
.jv-ub-btn {
	height: 26px;
	padding: 0 10px;
	border: 1px solid color-mix(in srgb, var(--link) 40%, transparent);
	border-radius: 7px;
	background: transparent;
	font-family: inherit;
	font-size: 12px;
	font-weight: 500;
	color: var(--link);
	cursor: pointer;
	white-space: nowrap;
}
.jv-ub-btn:hover {
	background: color-mix(in srgb, var(--link) 12%, transparent);
}
.jv-ub-btn--ghost {
	border-color: transparent;
	color: var(--text-2);
}
.jv-ub-btn--ghost:hover {
	background: color-mix(in srgb, var(--text) 8%, transparent);
}
.jv-ub-btn:focus-visible,
.jv-ub-x:focus-visible {
	outline: 2px solid var(--link);
	outline-offset: 2px;
}
.jv-ub-x {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 26px;
	height: 26px;
	border: none;
	border-radius: 7px;
	background: transparent;
	color: var(--text-2);
	cursor: pointer;
}
.jv-ub-x:hover {
	background: color-mix(in srgb, var(--text) 12%, transparent);
}
</style>
