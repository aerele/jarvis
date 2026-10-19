<script setup>
// The persistent card for one background export. Shared by the desktop and the phone app, so it
// uses plain elements and each app's own colour tokens (desktop first, phone as the fallback).
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { downloadUrl, exportCardView } from "../../lib/exportCardView.js";
import { cancelExport, exportJobs, refreshExports, watchExport } from "../../lib/exportJobs.js";
import { __ } from "../../lib/i18n.js";

const props = defineProps({ summary: { type: Object, required: true } });
let unsubscribe;
watch(
	() => props.summary.export_job,
	(id) => {
		unsubscribe?.();
		unsubscribe = watchExport(id);
	},
	{ immediate: true }
);

const root = ref(null);
const pending = ref(false);
const cancelError = ref("");
const now = ref(Date.now());
const entry = computed(() => exportJobs[props.summary.export_job]);
const state = computed(() => entry.value?.state);
const cancelBusy = computed(() => pending.value || !!state.value?.cancel_requested);
const view = computed(() =>
	exportCardView(props.summary, entry.value, { now: now.value, cancelling: cancelBusy.value })
);
const alertText = computed(() => cancelError.value || entry.value?.error || "");
const showBody = computed(
	() => view.value.track || view.value.stats.length || view.value.notes.length || alertText.value
);
const showFoot = computed(() => view.value.footer || view.value.buttons.length);
// One calm sentence for screen readers. Never the ticking timer or the row count.
const announcement = computed(() =>
	state.value === undefined
		? ""
		: [view.value.pill.text, view.value.notes[0]?.text].filter(Boolean).join(". ")
);
const downloadLabel = computed(() =>
	__("Download {0}", [state.value?.format ? `.${state.value.format}` : ""]).trim()
);

// The clock only runs while the job does; a queued or paused card does not count time.
let ticker;
watch(
	() => state.value?.status === "running",
	(running) => {
		clearInterval(ticker);
		ticker = null;
		if (!running) return;
		now.value = Date.now();
		ticker = setInterval(() => (now.value = Date.now()), 1000);
	},
	{ immediate: true }
);
onUnmounted(() => {
	unsubscribe?.();
	clearInterval(ticker);
});

// A control that unmounts (Cancel, Try again, Download) takes the keyboard focus with it, which
// drops it to the page. Look BEFORE the patch, while the old control is still focused, then
// hand focus to the card so the next Tab continues from here.
let hadFocus = false;
watch(
	() => [state.value?.status, entry.value?.error, cancelError.value],
	() => {
		hadFocus = !!root.value?.contains(document.activeElement);
		nextTick(() => {
			if (hadFocus && root.value && !root.value.contains(document.activeElement))
				root.value.focus();
		});
	},
	{ flush: "pre" }
);
// A newer answer from the server settles an old cancel failure.
watch(
	() => [state.value?.status, entry.value],
	() => (cancelError.value = "")
);

function downloadFile() {
	const url = downloadUrl(state.value);
	if (!url) return;
	const link = document.createElement("a");
	link.href = url;
	link.download = state.value.filename || "";
	document.body.appendChild(link);
	link.click();
	link.remove();
}
async function cancel() {
	if (cancelBusy.value) return;
	pending.value = true;
	cancelError.value = "";
	try {
		await cancelExport(props.summary.export_job);
	} catch (error) {
		cancelError.value = error.message;
	} finally {
		pending.value = false;
	}
}
</script>

<template>
	<section
		ref="root"
		class="jv-xcard"
		tabindex="-1"
		:aria-label="__('Export: {0}', [view.title])"
	>
		<span class="jv-sr" role="status">{{ announcement }}</span>
		<div class="jv-xhead">
			<span class="jv-xico" aria-hidden="true">
				<svg
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.7"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<rect x="4" y="3" width="16" height="18" rx="2" />
					<path d="M4 9h16M4 15h16M10 3v18" />
				</svg>
			</span>
			<div class="jv-xtitle">
				<b :title="view.title">{{ view.title }}</b>
				<span>{{ view.subtitle }}</span>
			</div>
			<span class="jv-pill" :class="view.pill.tone">{{ view.pill.text }}</span>
		</div>
		<div v-if="showBody" class="jv-xbody">
			<div v-if="view.track" class="jv-track" :class="{ indet: view.track === 'indet' }">
				<span class="jv-fill"></span>
			</div>
			<div v-if="view.stats.length" class="jv-stats">
				<span v-for="(stat, i) in view.stats" :key="i"
					><b v-if="stat.strong">{{ stat.strong }}</b
					>{{ stat.text }}</span
				>
			</div>
			<p v-for="(note, i) in view.notes" :key="i" class="jv-note" :class="note.tone">
				{{ note.text }}
			</p>
			<div v-if="alertText" class="jv-note bad jv-alert" role="alert">
				<span>{{ alertText }}</span>
				<button
					v-if="cancelError"
					type="button"
					class="jv-xbtn"
					data-testid="retry-cancel"
					@click="cancel"
				>
					{{ __("Try again") }}
				</button>
				<button
					v-else
					type="button"
					class="jv-xbtn"
					data-testid="refresh-export"
					@click="refreshExports()"
				>
					{{ __("Refresh") }}
				</button>
			</div>
		</div>
		<div v-if="showFoot" class="jv-xfoot">
			<small>{{ view.footer }}</small>
			<div class="jv-btns">
				<button
					v-if="view.buttons.includes('cancel')"
					type="button"
					class="jv-xbtn danger"
					data-testid="cancel-export"
					:aria-disabled="cancelBusy ? 'true' : null"
					:aria-label="cancelBusy ? __('Cancelling export') : __('Cancel export')"
					@click="cancel"
				>
					<svg
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						aria-hidden="true"
					>
						<path d="M6 6l12 12M18 6L6 18" />
					</svg>
					{{ cancelBusy ? __("Cancelling...") : __("Cancel") }}
				</button>
				<button
					v-if="view.buttons.includes('download')"
					type="button"
					class="jv-xbtn primary"
					data-testid="download-export"
					@click="downloadFile"
				>
					<svg
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M12 4v11M7 10l5 5 5-5M5 20h14" />
					</svg>
					{{ downloadLabel }}
				</button>
			</div>
		</div>
	</section>
</template>

<style scoped>
/* Tokens: the desktop app's first, the phone app's as the fallback. Tone TEXT is mixed toward the
   text colour so it passes 4.5:1 on the card; borders, fills and the track keep the pure tone. */
.jv-xcard {
	--x-surface: var(--surface, var(--card));
	--x-s1: var(--surface-1, var(--card2));
	--x-s2: var(--surface-2, var(--card2));
	--x-s3: var(--surface-3, var(--card3));
	--x-border: var(--border);
	--x-border2: var(--border-2, var(--border2));
	--x-text: var(--text, var(--ink9));
	--x-text2: var(--text-2, var(--ink7));
	--x-link: var(--link, var(--accent));
	--x-cta: var(--cta, var(--inv-bg));
	--x-cta-fg: var(--cta-fg, var(--inv-ink));
	--x-btn-bg: var(--cta-bg, var(--card2));
	--x-btn-bd: var(--cta-bd, var(--border2));
	--x-green-bd: var(--green-bd, color-mix(in srgb, var(--green) 30%, var(--border)));
	--x-red-bd: var(--red-bd, color-mix(in srgb, var(--red) 30%, var(--border)));
	--x-amber-bd: var(--amber-bd, color-mix(in srgb, var(--amber) 30%, var(--border)));
	--x-green-ink: color-mix(in srgb, var(--green) 70%, var(--x-text));
	--x-red-ink: color-mix(in srgb, var(--red) 70%, var(--x-text));
	--x-amber-ink: color-mix(in srgb, var(--amber) 70%, var(--x-text));
	--x-link-ink: color-mix(in srgb, var(--x-link) 70%, var(--x-text));

	container-type: inline-size;
	position: relative;
	margin: 12px 0;
	border: 1px solid var(--x-border2);
	border-radius: 12px;
	background: var(--x-surface);
	color: var(--x-text);
	font-size: 14px;
	line-height: 1.45;
	overflow: hidden;
	overflow-wrap: anywhere;
}
.jv-xcard:focus-visible,
.jv-xbtn:focus-visible {
	outline: 2px solid var(--x-link);
	outline-offset: 2px;
}
.jv-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	white-space: nowrap;
	border: 0;
}
.jv-xhead {
	display: flex;
	align-items: center;
	gap: 10px;
	padding: 12px 14px;
}
.jv-xico {
	flex: 0 0 auto;
	width: 34px;
	height: 34px;
	border-radius: 8px;
	display: grid;
	place-items: center;
	background: var(--x-s2);
	color: var(--x-text2);
}
.jv-xico svg {
	width: 18px;
	height: 18px;
}
.jv-xtitle {
	min-width: 0;
	flex: 1;
}
.jv-xtitle b {
	display: block;
	font-weight: 600;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-xtitle span {
	color: var(--x-text2);
	font-size: 12.5px;
}
.jv-pill {
	flex: 0 0 auto;
	font-size: 11.5px;
	font-weight: 500;
	padding: 3px 9px;
	border-radius: 999px;
	border: 1px solid var(--x-border2);
	color: var(--x-text2);
	background: var(--x-s1);
	white-space: nowrap;
}
.jv-pill.run {
	color: var(--x-link-ink);
	border-color: color-mix(in srgb, var(--x-link) 35%, var(--x-border));
	background: color-mix(in srgb, var(--x-link) 8%, var(--x-surface));
}
.jv-pill.ok {
	color: var(--x-green-ink);
	border-color: var(--x-green-bd);
	background: var(--green-bg);
}
.jv-pill.warn {
	color: var(--x-amber-ink);
	border-color: var(--x-amber-bd);
	background: var(--amber-bg);
}
.jv-pill.bad {
	color: var(--x-red-ink);
	border-color: var(--x-red-bd);
	background: var(--red-bg);
}
.jv-xbody {
	padding: 0 14px 12px;
	display: grid;
	gap: 10px;
}
.jv-track {
	height: 6px;
	border-radius: 99px;
	background: var(--x-s3);
	overflow: hidden;
	position: relative;
}
.jv-fill {
	position: absolute;
	inset: 0 auto 0 0;
	width: 0;
	background: var(--x-link);
	border-radius: 99px;
}
.jv-track.indet .jv-fill {
	width: 30%;
	animation: jv-xslide 1.4s ease-in-out infinite;
}
@keyframes jv-xslide {
	0% {
		left: -30%;
	}
	100% {
		left: 100%;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-track.indet .jv-fill {
		animation: none;
		left: 0;
		width: 100%;
		opacity: 0.35;
	}
}
.jv-stats {
	display: flex;
	flex-wrap: wrap;
	gap: 4px 18px;
	font-size: 12.5px;
	color: var(--x-text2);
	font-variant-numeric: tabular-nums;
}
.jv-stats b {
	color: var(--x-text);
	font-weight: 600;
}
.jv-note {
	margin: 0;
	font-size: 12.5px;
	color: var(--x-text2);
}
.jv-note.warn {
	color: var(--x-amber-ink);
}
.jv-note.bad {
	color: var(--x-red-ink);
}
.jv-alert {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
}
.jv-xfoot {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 10px 14px;
	border-top: 1px solid var(--x-border);
	background: var(--x-s1);
}
.jv-xfoot small {
	color: var(--x-text2);
	font-size: 12px;
}
.jv-btns {
	display: flex;
	gap: 8px;
}
.jv-xbtn {
	font: inherit;
	font-size: 13px;
	font-weight: 500;
	border-radius: 8px;
	padding: 6px 12px;
	border: 1px solid var(--x-btn-bd);
	background: var(--x-btn-bg);
	color: var(--x-text);
	cursor: pointer;
	display: inline-flex;
	align-items: center;
	gap: 6px;
}
.jv-xbtn.primary {
	background: var(--x-cta);
	color: var(--x-cta-fg);
	border-color: var(--x-cta);
}
.jv-xbtn.danger {
	color: var(--x-red-ink);
	border-color: var(--x-red-bd);
	background: var(--red-bg);
}
.jv-xbtn[aria-disabled="true"] {
	opacity: 0.6;
	cursor: default;
}
.jv-xbtn svg {
	width: 14px;
	height: 14px;
}
/* The phone layout follows the card's own width, so a narrow desktop column gets it too. */
@container (max-width: 520px) {
	.jv-xhead {
		padding: 10px 12px;
	}
	.jv-xbody {
		padding: 0 12px 10px;
	}
	.jv-xfoot {
		padding: 9px 12px;
	}
	.jv-xfoot small {
		flex-basis: 100%;
	}
	.jv-btns {
		width: 100%;
	}
	.jv-btns .jv-xbtn {
		flex: 1;
		justify-content: center;
		padding: 9px 12px;
		min-height: 36px;
	}
}
</style>
