<script setup>
import { computed, onMounted, ref } from "vue";
import { installPrompt, isStandalone } from "../install";
import { agentName } from "@/branding";
import BrandMark from "./BrandMark.vue";

// "Add Jarvis to your home screen." Two different worlds:
//  - Chrome/Android fires beforeinstallprompt. That event is captured in
//    src/install.js at module load, NOT here: it can fire in the same tick the
//    app mounts, so a listener in onMounted loses the race and the banner never
//    appears on a warm refresh. This component only reads the stashed event.
//  - iOS Safari has no such event and never will; Add to Home Screen is a manual
//    menu action, so there we can only tell the user where it is.

// Dismissal is deliberately NOT persisted, matching Frappe HR: closing the
// banner hides it for this page only, and it returns on the next load until the
// app is actually installed. A durable flag lost the install path for good on
// one stray tap of a small close button, with no way back short of clearing
// site data. isStandalone() is what ends it permanently.
const dismissed = ref(false);
const isIos = ref(false);

// A browser will not install a page it does not trust. `isSecureContext` is
// false on a plain-http LAN origin (http://192.168.x.x:8002 — how the bench is
// reached from a phone in dev), and on such an origin Chrome never fires
// beforeinstallprompt and navigator.serviceWorker is undefined. So the install
// offer vanishes with no explanation, which reads as a bug in the app. Say what
// is actually wrong instead. In production the app is only ever served over
// HTTPS, so this branch is dead there.
const insecure = ref(false);

// Show when we either hold a real prompt (Chrome), know we're on iOS, or need
// to explain why installing isn't possible here — unless the user closed it or
// the app is already installed.
const show = computed(
	() =>
		!dismissed.value &&
		!isStandalone() &&
		(!!installPrompt.value || isIos.value || insecure.value)
);

async function install() {
	const e = installPrompt.value;
	if (!e) return;
	e.prompt();
	await e.userChoice;
	// The event is single-use: once prompted it cannot be replayed.
	installPrompt.value = null;
}

function dismiss() {
	dismissed.value = true;
}

onMounted(() => {
	const ua = window.navigator.userAgent;
	if (/iPhone|iPad|iPod/.test(ua) && /Safari/.test(ua) && !/CriOS|FxiOS/.test(ua)) {
		isIos.value = true;
	}
	insecure.value = !window.isSecureContext;
});
</script>

<template>
	<Transition name="jv-install">
		<div v-if="show" class="jv-install">
			<BrandMark :size="34" />
			<div class="jv-install-text">
				<strong>Install {{ agentName }}</strong>
				<span v-if="insecure"
					>Open this site over https to install it. Browsers won't install an insecure
					page.</span
				>
				<!-- Draw the share glyph rather than naming it: Safari's control is an
				     unlabelled icon, so "tap Share" sends people hunting for a word that
				     is not on screen. -->
				<span v-else-if="isIos" class="jv-install-ios">
					Tap
					<svg
						class="jv-share-icon"
						viewBox="0 0 24 24"
						width="15"
						height="15"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
						role="img"
						aria-label="Share"
					>
						<path d="M12 3v12M8.5 6.5 12 3l3.5 3.5" />
						<path d="M6 13v6a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-6" />
					</svg>
					then “Add to Home Screen”.
				</span>
				<span v-else>Keep it one tap away, like an app.</span>
			</div>
			<button v-if="!isIos && !insecure" class="jv-install-cta" @click="install">
				Install
			</button>
			<button class="jv-icon-btn" aria-label="Dismiss" @click="dismiss">
				<svg
					viewBox="0 0 24 24"
					width="18"
					height="18"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
				>
					<path d="M18 6 6 18M6 6l12 12" />
				</svg>
			</button>
		</div>
	</Transition>
</template>

<style scoped>
/* In the layout flow at the very top — NOT floating. A fixed banner has to sit
   over something: at the bottom it buries the send button and the FAB, at the
   top it buries the first message. As a flex item it pushes the app down
   instead, so it can never cover content, and the row it occupies leaves with
   it when dismissed. */
.jv-install {
	flex: none;
	display: flex;
	align-items: center;
	gap: 12px;
	margin: 8px 12px 0;
	padding: 12px;
	border-radius: 14px;
	background: var(--card);
	border: 1px solid var(--border2);
}
.jv-install-text {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 2px;
	font-size: 13px;
	color: var(--ink6);
	line-height: 1.35;
}
.jv-install-text strong {
	font-size: 14px;
	font-weight: 600;
	color: var(--ink9);
}
/* The glyph is part of a sentence, so it rides the text baseline instead of
   stretching the line box. */
.jv-install-ios {
	display: inline;
}
.jv-share-icon {
	display: inline-block;
	vertical-align: -3px;
	margin: 0 1px;
}
.jv-install-cta {
	flex: none;
	padding: 9px 14px;
	border: 0;
	border-radius: 9px;
	background: var(--accent-solid);
	color: #fff;
	font: inherit;
	font-size: 14px;
	font-weight: 600;
	cursor: pointer;
}
.jv-install-enter-active,
.jv-install-leave-active {
	transition: opacity 0.2s ease, transform 0.2s ease;
}
.jv-install-enter-from,
.jv-install-leave-to {
	opacity: 0;
	transform: translateY(-12px);
}
</style>
