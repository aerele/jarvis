<!--
  The full-plan outline (P1, skill approve-and-run design §3.5) - PWA twin of
  the desktop's PlanOutline.vue. Hangs off an EXISTING confirmation card as
  `card.plan = {steps:[{n,verb,doctype,summary}]}`, additive, no new CARD_KIND.
  Rendered OUTSIDE the per-kind switch in PendingCard.vue.

  Logic (isDestructivePlanStep/PLAN_STEP_CAP) is shared via @shared so the two
  surfaces can never disagree on which steps still gate; the TEMPLATE is
  duplicated on purpose, same as PendingCard.vue itself - see its own header
  comment for why.
-->
<script setup>
import { computed } from "vue";
import { isDestructivePlanStep, PLAN_STEP_CAP } from "@shared/lib/actionSummary.js";

const props = defineProps({
	plan: { type: Object, required: true },
});

const steps = computed(() => (Array.isArray(props.plan?.steps) ? props.plan.steps : []));
const visibleSteps = computed(() => steps.value.slice(0, PLAN_STEP_CAP));
const extra = computed(() => Math.max(0, steps.value.length - visibleSteps.value.length));

function isVerified(step) {
	return step && step.verified != null ? !!step.verified : step && step.n === 1;
}
function verbLabel(verb) {
	const v = String(verb || "").trim();
	return v ? v.charAt(0).toUpperCase() + v.slice(1) : "Run";
}
</script>

<template>
	<div class="jv-po">
		<div class="jv-po-head">
			This run will do {{ steps.length }} step<template v-if="steps.length !== 1"
				>s</template
			>
		</div>
		<ol class="jv-po-list">
			<li
				v-for="s in visibleSteps"
				:key="s.n"
				class="jv-po-row"
				:class="{
					'jv-po-row--verified': isVerified(s),
					'jv-po-row--destructive': isDestructivePlanStep(s),
				}"
			>
				<span class="jv-po-n">{{ s.n }}.</span>
				<span class="jv-po-verb">{{ verbLabel(s.verb) }}</span>
				<span v-if="s.doctype" class="jv-po-dt">{{ s.doctype }}</span>
				<span v-if="s.summary" class="jv-po-summary">{{ s.summary }}</span>
				<span v-if="isVerified(s)" class="jv-po-tag jv-po-tag--ok">verified</span>
				<span v-else class="jv-po-tag">planned</span>
				<span v-if="isDestructivePlanStep(s)" class="jv-po-warn"
					>will still ask when reached</span
				>
			</li>
		</ol>
		<div v-if="extra > 0" class="jv-po-more">+{{ extra }} more</div>
	</div>
</template>

<style scoped>
.jv-po {
	margin-top: 12px;
	padding-top: 12px;
	border-top: 1px solid var(--border);
	font-size: 13px;
	color: var(--ink7);
}
.jv-po-head {
	font-weight: 600;
	color: var(--ink9);
	margin-bottom: 8px;
}
.jv-po-list {
	margin: 0;
	padding: 0;
	list-style: none;
	display: flex;
	flex-direction: column;
	gap: 6px;
	max-height: 280px;
	overflow-y: auto;
	overscroll-behavior: contain;
	-webkit-overflow-scrolling: touch;
}
.jv-po-row {
	display: flex;
	flex-wrap: wrap;
	align-items: baseline;
	gap: 7px;
	padding: 4px 0;
	color: var(--ink5);
}
.jv-po-n {
	flex: none;
	font-variant-numeric: tabular-nums;
}
.jv-po-verb {
	font-weight: 550;
	color: var(--ink7);
}
.jv-po-dt {
	color: var(--ink7);
}
.jv-po-summary {
	flex: 1 1 auto;
	min-width: 0;
	overflow-wrap: anywhere;
}
.jv-po-row--verified .jv-po-verb,
.jv-po-row--verified .jv-po-dt,
.jv-po-row--verified .jv-po-summary {
	color: var(--ink9);
}
.jv-po-tag {
	flex: none;
	font-size: 11px;
	font-weight: 600;
	letter-spacing: 0.3px;
	text-transform: uppercase;
	color: var(--ink5);
}
.jv-po-tag--ok {
	color: var(--green, var(--ink5));
}
.jv-po-row--destructive {
	background: var(--amber-bg, transparent);
	border-radius: 8px;
	padding-left: 6px;
	padding-right: 6px;
}
.jv-po-warn {
	flex: 1 0 100%;
	font-size: 11.5px;
	font-weight: 550;
	color: var(--amber, var(--ink5));
}
.jv-po-more {
	margin-top: 8px;
	color: var(--ink5);
	font-size: 12px;
}
</style>
