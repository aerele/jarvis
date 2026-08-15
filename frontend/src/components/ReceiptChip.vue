<!--
  Post-action receipt chip. A gated ERP write (create / update / submit / cancel
  / delete / amend / apply_workflow / send_email, single or bulk), once the user
  clicks Confirm or Discard on its confirmation card, is replaced by THIS durable
  chip in the transcript instead of the card just vanishing. Three outcomes:
    ✓ confirmed  — the write ran (green)
    ⊘ discarded  — the user declined; nothing ran (muted)
    ✗ failed     — confirmed but errored / rolled back (red)
  Fed by a role="tool" Jarvis Chat Message whose `action_outcome` is set. All
  wording + target links come from lib/actionSummary.receiptView (pure). Bulk
  shows a name teaser collapsed and the full linked list expanded; failures show
  the rolled-back reason behind a "why" toggle.
-->
<template>
	<div class="jv-receipt" :class="'jv-receipt--' + view.tone">
		<span class="jv-receipt-ico" :class="view.icon" aria-hidden="true">
			<svg
				v-if="view.icon === 'confirmed'"
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.4"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M20 6 9 17l-5-5" />
			</svg>
			<svg
				v-else-if="view.icon === 'discarded'"
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<circle cx="12" cy="12" r="9" />
				<path d="M5.6 5.6l12.8 12.8" />
			</svg>
			<svg
				v-else
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.4"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M18 6 6 18M6 6l12 12" />
			</svg>
		</span>
		<div class="jv-receipt-main">
			<div class="jv-receipt-line">
				<span class="jv-receipt-title">{{ view.title }}</span>
				<a
					v-if="singleUrl"
					:href="singleUrl"
					target="_blank"
					rel="noopener"
					class="jv-receipt-open"
					title="Open in Desk"
					>open ↗</a
				>
				<button
					v-if="hasWhy || hasList"
					class="jv-receipt-toggle"
					:aria-expanded="open"
					@click="open = !open"
				>
					{{ hasWhy ? "why" : "details" }}
					<svg
						class="jv-receipt-chev"
						:class="{ open }"
						width="11"
						height="11"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2.4"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M6 9l6 6 6-6" />
					</svg>
				</button>
				<span v-if="ts" class="jv-receipt-time">{{ ts }}</span>
			</div>
			<div v-if="hasList && !open" class="jv-receipt-teaser">{{ teaser }}</div>
			<div v-if="open && (hasWhy || hasList)" class="jv-receipt-detail">
				<div v-if="hasWhy" class="jv-receipt-error">{{ view.error }}</div>
				<div v-if="hasList" class="jv-receipt-targets">
					<template v-for="(t, i) in view.targets" :key="i">
						<a
							v-if="t.url"
							:href="t.url"
							target="_blank"
							rel="noopener"
							class="jv-receipt-link"
							>{{ t.name }}</a
						>
						<span v-else class="jv-receipt-tgt">{{ t.name }}</span>
					</template>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
import { computed, ref } from "vue";
import { receiptView } from "@/lib/actionSummary";

const props = defineProps({
	// A role="tool" Jarvis Chat Message with a non-empty action_outcome.
	message: { type: Object, required: true },
});

const open = ref(false);

function parseJson(v) {
	if (v == null) return null;
	if (typeof v === "object") return v;
	try {
		return JSON.parse(v);
	} catch (e) {
		return null;
	}
}

const view = computed(() => {
	const m = props.message;
	return receiptView(
		m.tool_name,
		parseJson(m.tool_args) || {},
		parseJson(m.tool_result),
		m.action_outcome
	);
});

// A single-record outcome with a Desk link → show a compact "open" affordance
// (the record name is already in the title). Bulk uses the details expander.
const singleUrl = computed(() => {
	const t = view.value.targets;
	return view.value.count === 1 && t.length === 1 && t[0].url ? t[0].url : "";
});
const hasWhy = computed(() => view.value.outcome === "failed" && !!view.value.error);
const hasList = computed(() => view.value.count > 1 && view.value.targets.length > 0);
const teaser = computed(() => {
	const names = view.value.targets.map((t) => t.name).filter(Boolean);
	if (names.length <= 3) return names.join(", ");
	return names.slice(0, 3).join(", ") + `, +${names.length - 3} more`;
});

const ts = computed(() => {
	const c = props.message.creation;
	if (!c) return "";
	const d = new Date(String(c).replace(" ", "T"));
	if (isNaN(d.getTime())) return "";
	return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
});
</script>

<style scoped>
.jv-receipt {
	display: flex;
	align-items: flex-start;
	gap: 9px;
	margin: 4px 0;
	padding: 8px 12px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-2);
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
}
.jv-receipt--success {
	border-color: color-mix(in srgb, var(--green) 32%, var(--border));
}
.jv-receipt--danger {
	border-color: var(--red-bd, color-mix(in srgb, var(--red) 32%, var(--border)));
	background: var(--red-bg, var(--surface-2));
}
.jv-receipt-ico {
	flex: none;
	margin-top: 1px;
	display: inline-flex;
}
.jv-receipt-ico.confirmed {
	color: var(--green);
}
.jv-receipt-ico.discarded {
	color: var(--text-3);
}
.jv-receipt-ico.failed {
	color: var(--red);
}
.jv-receipt-main {
	flex: 1;
	min-width: 0;
}
.jv-receipt-line {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 8px;
}
.jv-receipt-title {
	font-weight: 550;
	overflow-wrap: anywhere;
}
/* --link, not --cta: this is a link to the affected document. --cta is
   near-black, which made it indistinguishable from body text. */
.jv-receipt-open,
.jv-receipt-link {
	color: var(--link);
	text-decoration: none;
	font-size: 12px;
}
.jv-receipt-open:hover,
.jv-receipt-link:hover {
	text-decoration: underline;
}
.jv-receipt-toggle {
	display: inline-flex;
	align-items: center;
	gap: 3px;
	background: none;
	border: none;
	padding: 0;
	color: var(--text-3);
	font: inherit;
	font-size: 12px;
	cursor: pointer;
}
.jv-receipt-toggle:hover {
	color: var(--text-2);
}
.jv-receipt-chev {
	transition: transform 0.15s ease;
}
.jv-receipt-chev.open {
	transform: rotate(180deg);
}
.jv-receipt-time {
	margin-left: auto;
	flex: none;
	font-size: 11px;
	color: var(--text-3);
}
.jv-receipt-teaser {
	margin-top: 2px;
	font-size: 12px;
	color: var(--text-2);
	overflow-wrap: anywhere;
}
.jv-receipt-detail {
	margin-top: 6px;
}
.jv-receipt-error {
	font-size: 12px;
	color: var(--text-2);
	line-height: 1.5;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	margin-bottom: 6px;
}
.jv-receipt-targets {
	display: flex;
	flex-wrap: wrap;
	gap: 4px 12px;
}
.jv-receipt-tgt {
	font-size: 12px;
	color: var(--text-2);
}
</style>
