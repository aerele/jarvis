<script setup>
// The receipt of a decided server draft card, as one line in the thread: "Created Timesheet
// TS-0001" with Open and, for a submittable record, Submit. Presentational: lib/draftReceiptLines.js
// decides which rows draw one and what it says (the shared draftReceipt.js owns the words). The
// look is ActionCard's: its frame, and its "done" line (green check, 13px bold), with the amber and
// red tones of the phone's tokens for a partial and a failed one. Plain elements, like ActionCard.
defineProps({
	line: { type: Object, required: true },
	// The Submit step is in flight.
	busy: { type: Boolean, default: false },
	// The inline line under the receipt (a refused or failed Submit); no toast on the phone.
	note: { type: String, default: "" },
});
defineEmits(["submit"]);
</script>

<template>
	<div class="jv-receipt" :class="[`is-${line.tone}`, { 'is-muted': line.muted }]" role="status">
		<div class="jv-receipt-line">
			<svg
				v-if="line.tone === 'ok'"
				viewBox="0 0 24 24"
				width="15"
				height="15"
				fill="none"
				stroke="currentColor"
				stroke-width="2.4"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="M20 6 9 17l-5-5" />
			</svg>
			<svg
				v-else
				viewBox="0 0 24 24"
				width="15"
				height="15"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				aria-hidden="true"
			>
				<circle cx="12" cy="12" r="9" />
				<path d="M12 8v5M12 16h.01" />
			</svg>
			<span class="jv-receipt-text">{{ line.text }}</span>
			<a
				v-if="line.open"
				class="jv-receipt-open"
				:href="line.open.href"
				target="_blank"
				rel="noopener"
				>{{ line.open.label }}</a
			>
		</div>
		<p v-if="line.reason" class="jv-receipt-reason">{{ line.reason }}</p>
		<div v-if="line.submit" class="jv-receipt-next">
			<button
				type="button"
				class="jv-btn"
				:disabled="busy"
				@click="$emit('submit', line.submit)"
			>
				<span v-if="busy" class="jv-spinner" aria-hidden="true" />
				<span :class="{ 'jv-hide': busy }">{{ line.submit.label }}</span>
			</button>
		</div>
		<p v-if="note" class="jv-receipt-note" role="alert">{{ note }}</p>
	</div>
</template>

<style scoped>
.jv-receipt {
	margin-top: 8px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
	color: var(--green);
	font-size: 13px;
	line-height: 1.4;
}
.jv-receipt.is-warn {
	color: var(--amber);
}
.jv-receipt.is-error {
	color: var(--red);
}
.jv-receipt.is-muted {
	color: var(--ink7);
}
.jv-receipt-line {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 11px 12px;
	font-weight: 600;
}
.jv-receipt-line svg {
	flex: none;
}
.jv-receipt-text {
	min-width: 0;
	overflow-wrap: anywhere;
}
.jv-receipt-open {
	flex: none;
	margin-left: auto;
	padding: 8px 4px;
	color: var(--accent-solid);
	font-weight: 600;
	text-decoration: underline;
	text-underline-offset: 2px;
}
.jv-receipt-reason,
.jv-receipt-note {
	margin: 0;
	padding: 0 12px 10px;
	font-size: 12px;
	font-weight: 400;
	overflow-wrap: anywhere;
}
.jv-receipt-reason {
	color: var(--ink7);
}
.jv-receipt-note {
	color: var(--red);
}
.jv-receipt-next {
	display: flex;
	padding: 0 12px 10px;
}
.jv-btn {
	position: relative;
	display: grid;
	place-items: center;
	min-width: 92px;
	min-height: 40px;
	padding: 0 14px;
	border: 1px solid var(--border2);
	border-radius: 10px;
	background: var(--card);
	color: var(--ink8);
	font: inherit;
	font-size: 14px;
	font-weight: 600;
	cursor: pointer;
}
.jv-btn:disabled {
	opacity: 0.6;
}
.jv-hide {
	visibility: hidden;
}
.jv-spinner {
	position: absolute;
	width: 16px;
	height: 16px;
	border-radius: 50%;
	border: 2px solid var(--border2);
	border-top-color: var(--ink7);
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
