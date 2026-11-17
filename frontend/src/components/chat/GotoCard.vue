<script setup>
// The hand-off card ("Continue in Payment Calendar / Dashboards"). Pure presentation: ChatView owns
// the countdown (lib/gotoCountdown.js) and passes its copy in as `countdown`; this card only reports
// clicks and, while a countdown runs, whether the pointer or keyboard focus is inside it.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { PAUSE } from "@/lib/gotoCountdown";

const props = defineProps({
	// gotoCardCopy(): { title, button (null = information only), sub, icon: "calendar" | "chart" | "info", note }
	copy: { type: Object, required: true },
	// null, or { visible, srText, stay, openNow, openNowLabel } while a countdown runs
	countdown: { type: Object, default: null },
	// "Opening {0}…" between the open and the arrival (the countdown is over, the page is loading);
	// empty otherwise. The Open button is disabled meanwhile and there is no Stay.
	opening: { type: String, default: "" },
});
const emit = defineEmits(["open", "stay", "open-now", "pause", "resume"]);

const root = ref(null);
const openBtn = ref(null);
const openNowBtn = ref(null);

// Pause reasons this card has reported and not yet released.
const held = new Set();

function hold(reason) {
	if (!props.countdown || held.has(reason)) return;
	held.add(reason);
	emit("pause", reason);
}
function release(reason) {
	if (!held.delete(reason)) return;
	emit("resume", reason);
}
function releaseAll() {
	for (const reason of [...held]) release(reason);
}

function onFocusOut(e) {
	if (root.value?.contains(e.relatedTarget)) return;
	// The window lost focus (alt-tab) while focus stays on a card button: still inside the card.
	if (!document.hasFocus() && root.value?.contains(document.activeElement)) return;
	release(PAUSE.FOCUS);
}

// A touch screen keeps :hover on the last element tapped, so a sticky :hover only counts on a
// device that can hover. (No matchMedia: assume it can, as every desktop browser does.)
function isHovered(el) {
	try {
		const canHover =
			typeof window.matchMedia !== "function" || window.matchMedia("(hover: hover)").matches;
		return canHover && el.matches(":hover");
	} catch {
		return false;
	}
}

// The pointer or focus may already be inside when the countdown starts, or when a card that
// already carries one is mounted (a re-render replaced it): hold them now, no event will come.
function holdWhatIsInside() {
	const el = root.value;
	if (!el) return;
	if (isHovered(el)) hold(PAUSE.HOVER);
	if (el.contains(document.activeElement)) {
		const lostOpen = document.activeElement === openBtn.value;
		hold(PAUSE.FOCUS);
		if (lostOpen) nextTick(() => openNowBtn.value?.focus());
	}
}

// "pre" flush: the Open button is still in the DOM, so we can see whether it had focus before the
// countdown row replaces it.
watch(
	() => !!props.countdown,
	(counting) => {
		if (counting) holdWhatIsInside();
		else releaseAll();
	},
	{ flush: "pre" }
);
onMounted(() => {
	if (props.countdown) holdWhatIsInside();
});

onBeforeUnmount(releaseAll);

async function focusOpen() {
	if (!openBtn.value) await nextTick();
	openBtn.value?.focus();
}
defineExpose({ focusOpen });
</script>

<template>
	<div
		ref="root"
		class="jv-gcard"
		@pointerenter="hold(PAUSE.HOVER)"
		@pointerleave="release(PAUSE.HOVER)"
		@focusin="hold(PAUSE.FOCUS)"
		@focusout="onFocusOut"
	>
		<div class="jv-gcard-box">
			<div class="jv-gcard-head">
				<div class="jv-gcard-ic">
					<svg
						v-if="copy.note"
						data-icon="info"
						width="16"
						height="16"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.7"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<circle cx="12" cy="12" r="10" />
						<path d="M12 16v-4M12 8h.01" />
					</svg>
					<svg
						v-else-if="copy.icon === 'calendar'"
						data-icon="calendar"
						width="16"
						height="16"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.7"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<rect x="3" y="4" width="18" height="18" rx="2" />
						<path d="M16 2v4M8 2v4M3 10h18" />
					</svg>
					<svg
						v-else
						data-icon="chart"
						width="16"
						height="16"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.7"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M18 20V10M12 20V4M6 20v-6" />
					</svg>
				</div>
				<div class="jv-gcard-txt">
					<span class="jv-gcard-title">{{ copy.title }}</span>
					<span v-if="copy.sub" class="jv-gcard-sub">{{ copy.sub }}</span>
				</div>
				<button
					v-if="copy.button && !countdown"
					ref="openBtn"
					type="button"
					class="jv-gcard-btn"
					:disabled="!!opening"
					@click="emit('open')"
				>
					{{ copy.button }}
				</button>
			</div>
			<!-- Not a live region: the countdown's announcement already said it opens. -->
			<p v-if="opening && !countdown" class="jv-gcard-status jv-gcard-opening">
				{{ opening }}
			</p>
			<div v-if="countdown" class="jv-gcard-cd">
				<!-- Not a live region: ChatView announces once through announceSR. The ticking
				     number is visual only; the sr-only sentence is the complete status. -->
				<p class="jv-gcard-status">
					<span class="jv-gcard-vis" aria-hidden="true">{{ countdown.visible }}</span>
					<span class="jv-sr">{{ countdown.srText }}</span>
				</p>
				<div class="jv-gcard-actions">
					<button type="button" class="jv-gcard-stay" @click="emit('stay', $event)">
						{{ countdown.stay }}
					</button>
					<button
						ref="openNowBtn"
						type="button"
						class="jv-gcard-now"
						:aria-label="countdown.openNowLabel"
						@click="emit('open-now')"
					>
						{{ countdown.openNow }}
					</button>
				</div>
			</div>
		</div>
	</div>
</template>

<style scoped>
/* The wrapper is the size container: a container query only reaches its descendants. */
.jv-gcard {
	container-type: inline-size;
	width: 100%;
	max-width: 640px;
	margin-top: 12px;
}
.jv-gcard-box {
	display: flex;
	flex-direction: column;
	gap: 10px;
	padding: 11px 12px;
	border: 1px solid var(--border);
	background: var(--surface-1);
	border-radius: 11px;
}
.jv-gcard-head {
	display: flex;
	align-items: center;
	gap: 11px;
}
.jv-gcard-ic {
	flex: none;
	width: 34px;
	height: 34px;
	border-radius: 8px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: var(--cta-bg);
	color: var(--cta);
}
.jv-gcard-txt {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 1px;
}
.jv-gcard-title {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-gcard-sub {
	font-size: 11.5px;
	color: var(--text-3);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-gcard-btn,
.jv-gcard-stay,
.jv-gcard-now {
	flex: none;
	padding: 7px 13px;
	border-radius: 8px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	cursor: pointer;
	transition: opacity 0.12s;
}
.jv-gcard-btn,
.jv-gcard-now {
	background: var(--cta);
	border: 1px solid var(--cta);
	color: var(--cta-fg);
}
.jv-gcard-btn:hover,
.jv-gcard-now:hover {
	opacity: 0.9;
}
.jv-gcard-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
.jv-gcard-stay {
	background: transparent;
	border: 1px solid var(--border-2);
	color: var(--text);
}
.jv-gcard-stay:hover {
	background: var(--surface-2);
}
/* visible keyboard focus */
.jv-gcard-btn:focus-visible,
.jv-gcard-stay:focus-visible,
.jv-gcard-now:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* Dark mode: a light hover wash would glare on the dark card. */
.jv-dark .jv-gcard-stay:hover {
	background: var(--surface-3);
}
.jv-gcard-cd {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 10px;
}
.jv-gcard-status {
	position: relative;
	margin: 0;
	font-size: 12.5px;
	color: var(--text-2);
	font-variant-numeric: tabular-nums;
}
.jv-gcard-actions {
	display: flex;
	gap: 8px;
}
.jv-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	clip-path: inset(50%);
	white-space: nowrap;
	border: 0;
}
/* Narrow card: the status takes its own line, Stay left and Open now right below it, and the
   restated request wraps instead of being cut off. */
@container (max-width: 479px) {
	.jv-gcard-cd {
		flex-direction: column;
		align-items: stretch;
	}
	.jv-gcard-actions {
		justify-content: space-between;
	}
	.jv-gcard-stay,
	.jv-gcard-now {
		flex: 1;
		min-height: 44px;
	}
	.jv-gcard-head {
		flex-wrap: wrap;
	}
	.jv-gcard-title,
	.jv-gcard-sub {
		white-space: normal;
		overflow: visible;
		text-overflow: clip;
		overflow-wrap: anywhere;
	}
	.jv-gcard-btn {
		flex: 1 1 100%;
		min-height: 44px;
	}
}
</style>
