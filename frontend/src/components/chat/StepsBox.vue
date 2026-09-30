<template>
	<div v-if="visible" class="jv-steps-box">
		<!-- live: header (file turns only) + "+N earlier" + the step rows.
		     Visuals per plan Appendix B / the design canvas boards (Working,
		     FileGen, ExcelRead, Mobile). -->
		<div v-if="view.mode === 'live'" class="jv-steps-live">
			<div v-if="view.header" class="jv-steps-header">
				<svg
					v-if="view.header.kind === 'dashboard'"
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M18 20V10M12 20V4M6 20v-6" />
				</svg>
				<svg
					v-else-if="view.header.kind === 'pdf'"
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
					<path d="M14 2v6h6" />
				</svg>
				<svg
					v-else-if="view.header.kind === 'spreadsheet'"
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<rect x="3" y="3" width="18" height="18" rx="2" />
					<path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
				</svg>
				<svg
					v-else-if="view.header.kind === 'image'"
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<rect x="3" y="3" width="18" height="18" rx="2" />
					<circle cx="8.5" cy="8.5" r="1.5" />
					<path d="M21 15l-5-5L5 21" />
				</svg>
				<span class="jv-steps-header-title">{{ view.header.title }}</span>
			</div>

			<div v-if="view.earlier > 0" class="jv-steps-earlier">{{ earlierLabel }}</div>

			<!-- Keyed by the step's position in the whole turn, not in the window:
			     a new step gets a new row (and its fade) even once older ones fold
			     into "+N earlier", while narration still typing keeps its row. -->
			<div
				v-for="(row, i) in view.rows"
				:key="view.earlier + i"
				class="jv-steps-row"
				:class="`jv-steps-row-${row.state}`"
			>
				<span class="jv-steps-icon-col">
					<span class="jv-steps-icon">
						<svg
							v-if="row.state === 'done'"
							class="jv-steps-check"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-3)"
							stroke-width="2.4"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
						<span v-else-if="row.state === 'current'" class="jv-steps-dot" />
						<span v-else class="jv-steps-ring" />
					</span>
					<span v-if="i < view.rows.length - 1" class="jv-steps-connector" />
				</span>
				<span class="jv-steps-text-col">
					<span class="jv-steps-text">{{ row.text }}</span>
					<span v-if="row.state === 'current' && row.sub" class="jv-steps-sub">{{
						row.sub
					}}</span>
				</span>
			</div>

			<!-- one announcer for the whole turn: speaks the current step text on
			     change only, never on tool start/end or the elapsed timer. -->
			<span role="status" aria-live="polite" class="jv-steps-sr">{{ view.announce }}</span>
		</div>

		<!-- folded: "Worked 48s · 5 tools", expandable only when there is
		     something to expand into (foldedHead.expandable). -->
		<template v-else-if="view.mode === 'folded' && view.head">
			<component
				:is="headTag"
				:type="headTag === 'button' ? 'button' : null"
				:aria-expanded="view.head.expandable ? open : null"
				class="jv-steps-head"
				@click="onHeadClick"
			>
				<svg
					v-if="view.head.expandable"
					class="jv-steps-chev"
					:class="{ open }"
					width="12"
					height="12"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="m9 18 6-6-6-6" />
				</svg>
				<span class="jv-steps-head-body">
					<span class="jv-steps-head-line">
						<!-- "·" only between parts that exist, so the line never starts
						     with one when a part (say, the time) is missing. -->
						<template v-for="(part, i) in headParts" :key="part.cls">
							<span v-if="i" class="jv-steps-head-count"> · </span>
							<span :class="part.cls">{{ part.text }}</span>
						</template>
					</span>
					<span v-if="view.head.subline" class="jv-steps-head-subline">{{
						view.head.subline
					}}</span>
				</span>
			</component>
			<div v-if="open && view.head.expandable" class="jv-steps-body">
				<slot name="details" />
			</div>
		</template>

		<!-- queued: the only line of a turn waiting for a slot. Cancel copies
		     today's queued chip button (ChatView.vue .jv-queued-cancel, grep
		     queuedChipLabel), which is a plain underlined button, not frappe-ui. -->
		<div v-else-if="view.mode === 'queued'" class="jv-steps-queued-row">
			<span class="jv-steps-ring" />
			<span class="jv-steps-queued-label">{{ view.label }}</span>
			<button type="button" class="jv-steps-cancel" @click="$emit('cancel')">Cancel</button>
		</div>
	</div>
</template>

<script setup>
// StepsBox: the one box a running (or just-finished) turn shows in its reply
// row's #above-body (plan .claude/workflow/plans/2026-09-28-live-turn-steps.md,
// Appendix B). Pure rendering of the view liveTurn.js's liveBox()/foldedHead()
// produce; no state of its own beyond the `open` prop ChatView already tracks.
import { computed } from "vue";

const props = defineProps({
	view: { type: Object, required: true },
	open: { type: Boolean, default: false },
});
const emit = defineEmits(["toggle", "cancel"]);

const visible = computed(() => {
	const v = props.view;
	if (!v) return false;
	if (v.mode === "folded") return !!v.head;
	return v.mode === "live" || v.mode === "queued";
});

const headTag = computed(() => (props.view.head?.expandable ? "button" : "div"));

const headParts = computed(() => {
	const h = props.view.head;
	if (!h) return [];
	return [
		h.label && { cls: "jv-steps-head-label", text: h.label },
		h.count && { cls: "jv-steps-head-count", text: h.count },
		h.finishing && { cls: "jv-steps-head-finishing", text: "finishing" },
	].filter(Boolean);
});

function onHeadClick() {
	if (props.view.head && props.view.head.expandable) emit("toggle");
}

const earlierLabel = computed(() => {
	const n = props.view.earlier || 0;
	return `+${n} earlier step${n === 1 ? "" : "s"}`;
});
</script>

<style scoped>
.jv-steps-box {
	margin: 0 0 10px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
	overflow: hidden;
}

/* ---- live ---------------------------------------------------------- */
.jv-steps-live {
	padding: 14px 16px 12px;
	display: flex;
	flex-direction: column;
}
.jv-steps-header {
	display: flex;
	align-items: center;
	gap: 8px;
	padding-bottom: 11px;
	margin-bottom: 12px;
	border-bottom: 1px solid var(--border);
	font-size: 12.5px;
	font-weight: 600;
	color: var(--text);
}
.jv-steps-header svg {
	flex: none;
	color: var(--text-2);
}
.jv-steps-earlier {
	padding: 0 0 8px 28px;
	font-size: 12px;
	color: var(--text-3);
}
.jv-steps-row {
	display: flex;
	gap: 12px;
	animation: jv-steps-fade-in 0.25s ease-out;
}
.jv-steps-icon-col {
	width: 16px;
	flex: none;
	display: flex;
	flex-direction: column;
	align-items: center;
}
.jv-steps-icon {
	height: 20px;
	flex: none;
	display: flex;
	align-items: center;
	justify-content: center;
}
.jv-steps-check {
	width: 13px;
	height: 13px;
}
.jv-steps-dot {
	width: 8px;
	height: 8px;
	border-radius: 50%;
	background: var(--brand-2);
	animation: jv-steps-pulse 1.4s ease-in-out infinite;
}
.jv-steps-ring {
	width: 8px;
	height: 8px;
	box-sizing: border-box;
	border-radius: 50%;
	border: 1.5px solid var(--border-2);
}
.jv-steps-connector {
	width: 1px;
	flex-grow: 1;
	background: var(--border-2);
}
.jv-steps-text-col {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 3px;
}
.jv-steps-row:not(:last-child) .jv-steps-text-col {
	padding-bottom: 12px;
}
.jv-steps-text {
	line-height: 20px;
}
.jv-steps-row-done .jv-steps-text {
	font-size: 13px;
	color: var(--text-2);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-steps-row-current .jv-steps-text {
	font-size: 13.5px;
	font-weight: 500;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-steps-row-upcoming .jv-steps-text {
	font-size: 13px;
	color: var(--text-3);
}
.jv-steps-sub {
	font-size: 12px;
	color: var(--text-3);
	animation: jv-steps-shim 1.8s ease-in-out infinite;
}
@media (max-width: 640px) {
	.jv-steps-row-current .jv-steps-text {
		white-space: normal;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
}

/* ---- folded (also the saved activity strip look, ChatView .jv-activity-*) */
.jv-steps-head {
	display: flex;
	align-items: center;
	gap: 7px;
	width: 100%;
	padding: 7px 11px;
	background: transparent;
	border: none;
	font-family: inherit;
	font-size: 12px;
	color: var(--text-2);
	text-align: left;
}
button.jv-steps-head {
	cursor: pointer;
}
button.jv-steps-head:hover {
	background: var(--surface-2);
}
.jv-steps-chev {
	flex: none;
	color: var(--text-3);
	transition: transform 0.15s ease;
}
.jv-steps-chev.open {
	transform: rotate(90deg);
}
.jv-steps-head-body {
	display: flex;
	flex-direction: column;
	gap: 2px;
	min-width: 0;
}
.jv-steps-head-line {
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-steps-head-label {
	font-weight: 600;
	color: var(--text);
}
.jv-steps-head-count {
	color: var(--text-3);
}
.jv-steps-head-finishing {
	color: var(--text-3);
	animation: jv-steps-shim 1.8s ease-in-out infinite;
}
.jv-steps-head-subline {
	font-size: 11.5px;
	color: var(--text-3);
}
.jv-steps-body {
	border-top: 1px solid var(--border);
	padding: 5px;
	display: flex;
	flex-direction: column;
	gap: 4px;
}

/* ---- queued ---------------------------------------------------------- */
.jv-steps-queued-row {
	display: flex;
	align-items: center;
	gap: 12px;
	padding: 6px 8px 6px 16px;
}
.jv-steps-queued-label {
	flex: 1;
	min-width: 0;
	font-size: 13px;
	color: var(--text-2);
}
.jv-steps-cancel {
	appearance: none;
	flex: none;
	background: transparent;
	border: none;
	padding: 2px 6px;
	margin: 0;
	font: inherit;
	font-size: 12px;
	color: var(--text-3);
	text-decoration: underline;
	cursor: pointer;
	border-radius: 5px;
}
.jv-steps-cancel:hover {
	color: var(--text);
	background: var(--surface-2);
}

/* ---- shared: the visually-hidden announcer (ChatView .jv-sr, copied since
       scoped styles do not cross component boundaries) -------------------- */
.jv-steps-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	border: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	clip-path: inset(50%);
	white-space: nowrap;
}

@keyframes jv-steps-pulse {
	0%,
	100% {
		transform: scale(1);
		opacity: 1;
	}
	50% {
		transform: scale(0.6);
		opacity: 0.55;
	}
}
@keyframes jv-steps-shim {
	0%,
	100% {
		opacity: 0.55;
	}
	50% {
		opacity: 1;
	}
}
@keyframes jv-steps-fade-in {
	from {
		opacity: 0;
		transform: translateY(4px);
	}
	to {
		opacity: 1;
		transform: translateY(0);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-steps-dot,
	.jv-steps-sub,
	.jv-steps-head-finishing,
	.jv-steps-row {
		animation: none;
	}
}
</style>
