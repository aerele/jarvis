<template>
	<section
		id="day-panel"
		class="ledger-panel"
		aria-live="polite"
		:aria-busy="panel.loading ? 'true' : undefined"
	>
		<div class="ledger-heading">
			<div>
				<span class="panel-eyebrow">{{
					scope === "date" ? "SELECTED DATE" : "CURRENT OBLIGATIONS"
				}}</span>
				<h2 id="ledger-heading" tabindex="-1">{{ title }}</h2>
				<p>{{ subline }}</p>
			</div>
			<button
				v-if="scope !== 'date'"
				id="back-to-date"
				type="button"
				class="ledger-back"
				@click="emit('back')"
			>
				Back to date
			</button>
		</div>
		<LedgerTabs :tabs="tabs" :side="side" prefix="panel" @select="emit('side', $event)" />
		<div class="ledger-list">
			<p v-if="panel.loading" class="ledger-note" role="status">Loading invoices…</p>
			<div v-else-if="panel.error" class="ledger-note" role="alert">
				{{ explain(panel.error.code) }}
				<button type="button" class="ledger-retry" @click="emit('retry')">Retry</button>
			</div>
			<p v-else-if="unknown" class="ledger-note">
				Invoice data is unavailable for this month.
			</p>
			<p v-else-if="!panel.rows.length" class="ledger-note">
				No {{ side === "Payable" ? "supplier" : "customer" }} invoices for this selection.
			</p>
			<template v-else>
				<div v-for="row in panel.rows" :key="row.id" class="ledger-row">
					<div class="ledger-party">
						<div class="invoice-party">{{ row.party_name || row.party }}</div>
						<span class="invoice-reference">{{ row.voucher_no }}</span>
						<small
							>{{ row.due_date ? "Due " : ""
							}}{{ dueLabel(row.due_date, anchor) }}</small
						>
					</div>
					<div class="invoice-balance">
						<strong>{{ money(row.remaining) }}</strong>
						<small>{{ row.settlement }}</small>
					</div>
				</div>
			</template>
		</div>
		<button
			id="panel-view-all"
			type="button"
			class="ledger-more"
			:disabled="count === null"
			@click="emit('view-all')"
		>
			{{ count === null ? "View all records" : `View all ${count} records` }}
			<ArrowIcon />
		</button>
	</section>
</template>
<script setup>
import { computed } from "vue";
import { snapshotMoney } from "@/lib/paymentCalendar/format";
import ArrowIcon from "./ArrowIcon.vue";
import LedgerTabs from "./LedgerTabs.vue";
import { explain } from "./messages";
import { dueLabel, panelSubline, scopeTitle, sideTabs } from "./ledgerCopy";
import "./tokens.css";
import "./DayLedger.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	scope: { type: String, required: true },
	side: { type: String, required: true },
	date: { type: String, required: true },
	// One area of usePaymentLedger: rows, total, loading, error, unavailable.
	panel: { type: Object, required: true },
});
const emit = defineEmits(["side", "back", "view-all", "retry"]);

const anchor = computed(() => props.snapshot.scope.anchor);
const title = computed(() => scopeTitle(props.scope, { date: props.date, anchor: anchor.value }));
const subline = computed(() => panelSubline(props.scope, props.snapshot.generated_at));
const tabs = computed(() => sideTabs(props.snapshot, props.scope, props.date));
const activeTab = computed(() => tabs.value.find((tab) => tab.side === props.side));
// The record count is the server's: the loaded total, else the tab's own count.
const count = computed(() => {
	if (props.panel.unavailable) return null;
	if (Number.isInteger(props.panel.total)) return props.panel.total;
	return activeTab.value?.known ? Number(activeTab.value.count) : null;
});
const unknown = computed(
	() =>
		props.panel.unavailable ||
		(!!activeTab.value && !activeTab.value.known && !props.panel.rows.length)
);
const money = (value) => snapshotMoney(props.snapshot).exact(value);
</script>
