<!--
  Post-action receipt chip. A gated ERP write (create / update / submit / cancel
  / delete / amend / apply_workflow / send_email, single or bulk), once the user
  clicks Confirm or Discard on its confirmation card, is replaced by THIS durable
  chip in the transcript instead of the card just vanishing. Outcomes:
    ✓ confirmed              — the write ran (green)
    ⚡ auto_applied           — ran uncarded under an armed macro/skill (green)
    ⊘ discarded              — the user declined; nothing ran (muted)
    ⊘ cancelled/superseded/expired — never answered; nothing ran (muted)
    ✗ failed                 — confirmed but errored / rolled back (red)
    ! unknown/partial        — outcome unverified; check before retrying (amber)
  Fed by a role="tool" Jarvis Chat Message whose `action_outcome` is set. All
  wording + target links come from lib/actionSummary.receiptView (pure), which
  also NEUTRALIZES any outcome it doesn't recognise to the "unknown" chip -
  never the confirmed/✓ path. Bulk shows a name teaser collapsed and the full
  linked list expanded; failures show the rolled-back reason behind a "why"
  toggle, and a failed / partial / unknown chip its confirmation's reference.
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
				v-else-if="['discarded', 'cancelled', 'superseded', 'expired'].includes(view.icon)"
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
				v-else-if="['unknown', 'partial'].includes(view.icon)"
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<circle cx="12" cy="12" r="9" />
				<path d="M12 8v5" />
				<path d="M12 16.5h.01" />
			</svg>
			<!-- Applied by the chat's auto mode (#581): the composer toggle's own icon,
			     so the receipt and the control that caused it read as one thing. -->
			<svg
				v-else-if="autoModeApplied"
				class="jv-receipt-automode"
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="currentColor"
				stroke="currentColor"
				stroke-width="1.6"
				stroke-linejoin="round"
			>
				<path d="M5 7.5v9l6-4.5z" />
				<path d="M13.5 7.5v9l6-4.5z" />
			</svg>
			<svg
				v-else-if="view.icon === 'auto_applied'"
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M13 2 3 14h7l-1 8 10-12h-7z" />
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
				<span
					v-if="armedMacro"
					class="jv-receipt-armed"
					:title="'Ran automatically under the armed macro ' + armedMacro"
					>· {{ armedMacro }}</span
				>
				<span
					v-if="armedSkill"
					class="jv-receipt-armed"
					:title="'Ran automatically under the approved skill run ' + armedSkill"
					>· {{ armedSkill }}</span
				>
				<span
					v-if="requestApproved"
					class="jv-receipt-armed"
					title="Ran automatically because you approved this request (confirm all)"
					>· you approved this request</span
				>
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
			<FailureReference :id="view.reference" />
			<!-- Next step offered after a draft was created (#621): opens the normal
			     confirm card; nothing runs from this button. -->
			<div v-if="nextStep" class="jv-receipt-next">
				<Button
					size="sm"
					variant="outline"
					:loading="nextBusy"
					@click="$emit('next-action', nextStep)"
					>{{ nextStep.label }}</Button
				>
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
import { Button } from "frappe-ui";
import { __ } from "@/lib/i18n";
import { receiptView } from "@/lib/actionSummary";
import FailureReference from "./FailureReference.vue";

const props = defineProps({
	// A role="tool" Jarvis Chat Message with a non-empty action_outcome.
	message: { type: Object, required: true },
	// The chat runs in auto mode (#581): its writes apply without a card because of
	// the chat, not a per-request approval, so the "confirm all" line below is false.
	autoMode: { type: Boolean, default: false },
	// The suggested next step was already acted on (the thread holds a later
	// submit / workflow receipt for this record): the parent hides the button.
	nextDone: { type: Boolean, default: false },
	nextBusy: { type: Boolean, default: false },
});
defineEmits(["next-action"]);

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

// Provenance for an armed-macro receipt: which macro authorized running this write
// without a confirmation card. Shown on both auto_applied (ran ok) and a failed
// armed write (labelled "failed" but still carrying the provenance).
const armedMacro = computed(() => props.message.armed_by_macro || "");
// Provenance for an approved-skill-run receipt: which armed `/slug` skill's
// "Approve & run" authorized this write without its own confirmation card.
// Mutually exclusive with armedMacro (jarvis/api.py never sets both).
const armedSkill = computed(() => props.message.armed_by_skill || "");
// Request-scoped "confirm all" (design Layer B): ran uncarded because the user
// approved the whole request. Unlike a macro/skill run there is no external armer
// NAME, so a bare auto_applied chip would carry no "why"; this fallback labels it.
// Only for an auto_applied outcome with neither macro nor skill provenance, and
// never in an auto-mode chat, where "Applied automatically" alone is the truth.
const requestApproved = computed(
	() =>
		view.value.icon === "auto_applied" &&
		!armedMacro.value &&
		!armedSkill.value &&
		!props.autoMode
);
// The same receipt in an auto-mode chat: applied by the chat's auto mode, so it
// carries the toggle's icon. A macro or skill run keeps the bolt even there.
const autoModeApplied = computed(
	() =>
		view.value.icon === "auto_applied" &&
		!armedMacro.value &&
		!armedSkill.value &&
		props.autoMode
);

// A single-record outcome with a Desk link → show a compact "open" affordance
// (the record name is already in the title). Bulk uses the details expander.
const singleUrl = computed(() => {
	const t = view.value.targets;
	return view.value.count === 1 && t.length === 1 && t[0].url ? t[0].url : "";
});
// {kind: "workflow"|"submit", action, label} from the create receipt's result,
// plus the record it applies to. Only a confirmed create carries one.
const nextStep = computed(() => {
	if (props.nextDone || view.value.outcome !== "confirmed") return null;
	const data = (parseJson(props.message.tool_result) || {}).data || {};
	const s = data.suggested_next;
	if (!s || !s.kind || !data.doctype || !data.name) return null;
	// Worded here (and translatable), never persisted. Old rows carried a label.
	const label = s.kind === "submit" ? __("Submit") : s.action || s.label;
	if (!label) return null;
	return {
		kind: s.kind,
		action: s.action || null,
		label,
		doctype: data.doctype,
		name: data.name,
	};
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
.jv-receipt--warning {
	border-color: var(--amber-bd, color-mix(in srgb, var(--amber) 32%, var(--border)));
	background: var(--amber-bg, var(--surface-2));
}
.jv-receipt-ico {
	flex: none;
	margin-top: 1px;
	display: inline-flex;
}
.jv-receipt-ico.confirmed {
	color: var(--green);
}
.jv-receipt-ico.auto_applied {
	color: var(--green);
}
.jv-receipt-armed {
	flex: 0 1 auto;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 11px;
	color: var(--text-3);
}
.jv-receipt-ico.discarded,
.jv-receipt-ico.cancelled,
.jv-receipt-ico.superseded,
.jv-receipt-ico.expired {
	color: var(--text-3);
}
.jv-receipt-ico.failed {
	color: var(--red);
}
.jv-receipt-ico.unknown,
.jv-receipt-ico.partial {
	color: var(--amber);
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
.jv-receipt-next {
	margin-top: 6px;
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
