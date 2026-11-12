<template>
	<section class="pcal-headlines" aria-label="Cash flow headline metrics">
		<article
			v-for="card in cards"
			:id="card.id"
			:key="card.id"
			class="headline-card"
			:data-state="card.state"
			:data-cause="card.cause"
			:aria-busy="card.loading ? 'true' : undefined"
		>
			<div class="headline-title">
				<h2>{{ card.title }}</h2>
				<span v-if="card.period" :id="card.periodId" class="metric-period">{{
					card.period
				}}</span>
				<span v-else-if="card.pill" :id="card.pillId" class="metric-state">{{
					card.pill
				}}</span>
			</div>
			<strong
				:id="card.valueId"
				class="headline-value"
				:class="{ skeleton: card.loading }"
				>{{ card.value }}</strong
			>
			<p :id="card.noteId" class="headline-note">{{ card.note }}</p>
			<div class="headline-foot">
				<span>{{ card.footer }}</span>
				<button
					:id="card.buttonId"
					type="button"
					:data-obligation="card.obligation"
					:disabled="card.actionDisabled"
					@click="card.run"
				>
					{{ card.action }}
				</button>
			</div>
			<small v-if="card.assumption" class="metric-assumption">{{ card.assumption }}</small>
		</article>
	</section>
</template>
<script setup>
import { computed } from "vue";
import { immediateCard, lowestCard, monthCard } from "./headlineCards";
import "./tokens.css";
import "./CalendarHeadlines.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	// The displayed month (YYYY-MM); empty skips the stale-snapshot check.
	month: { type: String, default: "" },
});
const emit = defineEmits(["jump", "view-totals", "obligation"]);

const cards = computed(() => {
	const lowest = lowestCard(props.snapshot),
		month = monthCard(props.snapshot, props.month),
		immediate = immediateCard(props.snapshot);
	return [
		{
			...lowest,
			id: "lowest-card",
			title: "Lowest cushion",
			pillId: "lowest-status",
			valueId: "lowest-cushion",
			noteId: "lowest-note",
			buttonId: "trough-jump",
			assumption: "Assumes scheduled collections arrive on time",
			run: () => lowest.date && emit("jump", lowest.date),
		},
		{
			...month,
			id: "month-flow-card",
			title: "Month net flow",
			periodId: "month-flow-scope",
			valueId: "month-flow",
			noteId: "month-flow-note",
			buttonId: "month-flow-details",
			footer: "Expected inflows − scheduled outflows",
			assumption: "Remaining balances · Not cash received",
			run: () => emit("view-totals"),
		},
		{
			...immediate,
			id: "immediate-card",
			title: "Immediate dues",
			pillId: "immediate-status",
			valueId: "immediate-dues",
			footer: "Excludes dues scheduled today",
			obligation: "immediate",
			run: () => emit("obligation", "immediate"),
		},
	];
});
</script>
