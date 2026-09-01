<!--
  The full-plan outline (P1, skill approve-and-run design §3.5): hangs off an
  EXISTING confirmation card as `card.plan = {steps:[{n,verb,doctype,summary}]}`
  - additive, no new CARD_KIND. Rendered OUTSIDE the per-kind switch in
  PendingCard.vue, so it shows beside whatever kind step 1 actually is.

  This is the agent's DECLARED intent for the whole run, not a fabricated
  preview of steps 2..N - only step 1 (the card it rides on) got a real
  dry-run. A step still gates when the agent reaches it iff the SERVER
  classifies its verb as destructive (delete/cancel/amend); that classification
  drives the "will still ask when reached" cue here too, via the shared
  isDestructivePlanStep - never the model's own caption for the step.
-->
<script setup>
import { computed } from "vue";
import { isDestructivePlanStep, PLAN_STEP_CAP } from "@/lib/actionSummary";

const props = defineProps({
	// {steps:[{n, verb, doctype, summary, verified?}]}
	plan: { type: Object, required: true },
});

const steps = computed(() => (Array.isArray(props.plan?.steps) ? props.plan.steps : []));
const visibleSteps = computed(() => steps.value.slice(0, PLAN_STEP_CAP));
const extra = computed(() => Math.max(0, steps.value.length - visibleSteps.value.length));

// Step 1 is the one real preview (the card this outline rides on); the server
// may also say so explicitly (`verified`) for a step this client did not itself
// number as 1 - trust the data when it distinguishes, else fall back to "n===1".
function isVerified(step) {
	return step && step.verified != null ? !!step.verified : step && step.n === 1;
}
function verbLabel(verb) {
	const v = String(verb || "").trim();
	return v ? v.charAt(0).toUpperCase() + v.slice(1) : "Run";
}
</script>

<template>
	<div class="jv-plan">
		<div class="jv-plan-head">
			This run will do {{ steps.length }} step<template v-if="steps.length !== 1"
				>s</template
			>
		</div>
		<ol class="jv-plan-list">
			<li
				v-for="s in visibleSteps"
				:key="s.n"
				class="jv-plan-row"
				:class="{
					'jv-plan-row--verified': isVerified(s),
					'jv-plan-row--destructive': isDestructivePlanStep(s),
				}"
			>
				<span class="jv-plan-n">{{ s.n }}.</span>
				<span class="jv-plan-verb">{{ verbLabel(s.verb) }}</span>
				<span v-if="s.doctype" class="jv-plan-dt">{{ s.doctype }}</span>
				<span v-if="s.summary" class="jv-plan-summary">{{ s.summary }}</span>
				<span v-if="isVerified(s)" class="jv-plan-tag jv-plan-tag--ok">verified</span>
				<span v-else class="jv-plan-tag">planned</span>
				<span v-if="isDestructivePlanStep(s)" class="jv-plan-warn"
					>will still ask when reached</span
				>
			</li>
		</ol>
		<div v-if="extra > 0" class="jv-plan-more">+{{ extra }} more</div>
	</div>
</template>

<style scoped>
.jv-plan {
	margin-top: 10px;
	padding-top: 10px;
	border-top: 1px solid var(--border);
	font-size: 12.5px;
	color: var(--text);
}
.jv-plan-head {
	font-weight: 600;
	margin-bottom: 6px;
}
.jv-plan-list {
	margin: 0;
	padding: 0;
	list-style: none;
	display: flex;
	flex-direction: column;
	gap: 4px;
	max-height: 260px;
	overflow-y: auto;
	overscroll-behavior: contain;
}
.jv-plan-row {
	display: flex;
	flex-wrap: wrap;
	align-items: baseline;
	gap: 6px;
	padding: 3px 0;
	color: var(--text-3);
}
.jv-plan-n {
	flex: none;
	font-variant-numeric: tabular-nums;
}
.jv-plan-verb {
	font-weight: 550;
	color: var(--text-2);
}
.jv-plan-dt {
	color: var(--text-2);
}
.jv-plan-summary {
	flex: 1 1 auto;
	min-width: 0;
	overflow-wrap: anywhere;
}
/* The one step with a real dry-run preview (usually #1, the card this outline
   rides on) reads as settled fact; the rest read as declared intent. */
.jv-plan-row--verified .jv-plan-verb,
.jv-plan-row--verified .jv-plan-dt,
.jv-plan-row--verified .jv-plan-summary {
	color: var(--text);
}
.jv-plan-tag {
	flex: none;
	font-size: 10.5px;
	font-weight: 600;
	letter-spacing: 0.3px;
	text-transform: uppercase;
	color: var(--text-3);
}
.jv-plan-tag--ok {
	color: var(--green, var(--text-3));
}
/* A destructive step (delete/cancel/amend) still gates on its own turn even
   under an approved run - say so right on the row, not just in the fine print. */
.jv-plan-row--destructive {
	background: var(--amber-bg, transparent);
	border-radius: 6px;
	padding-left: 4px;
	padding-right: 4px;
}
.jv-plan-warn {
	flex: 1 0 100%;
	font-size: 11px;
	font-weight: 550;
	color: var(--amber, var(--text-3));
}
.jv-plan-more {
	margin-top: 6px;
	color: var(--text-3);
	font-size: 11.5px;
}
</style>
