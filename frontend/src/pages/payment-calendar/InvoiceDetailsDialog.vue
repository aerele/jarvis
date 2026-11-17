<template>
	<PcalModal
		:model-value="modal.open"
		:title="title"
		close-label="Close invoice details"
		close-id="close-details"
		size="lg"
		@update:model-value="(open) => !open && emit('close')"
	>
		<p class="modal-context">{{ context }}</p>
		<LedgerTabs
			:tabs="tabs"
			:side="modal.side"
			prefix="modal"
			@select="emit('side', $event)"
		/>
		<div class="sort-bar">
			<span>{{ summary }}</span>
			<label
				>Sort
				<select
					id="record-sort"
					aria-label="Sort invoice records"
					:value="templateKey"
					@change="chooseSort($event.target.value)"
				>
					<option v-for="[key, name] in SORT_OPTIONS" :key="key" :value="key">
						{{ name }}
					</option>
				</select></label
			>
		</div>
		<div class="table-wrap">
			<table class="detail-table">
				<thead>
					<tr>
						<th
							v-for="column in COLUMNS"
							:key="column.title"
							v-bind="headAttrs(column)"
						>
							<button
								v-if="column.sort"
								type="button"
								:data-sort="column.sort"
								@click="chooseSort(column.sort)"
							>
								{{ column.title }}{{ arrow(column.sort) }}
							</button>
							<template v-else>{{ column.title }}</template>
						</th>
					</tr>
				</thead>
				<tbody>
					<tr v-if="notice">
						<td class="empty-table" colspan="6">
							<div v-if="modal.error" role="alert">
								{{ notice }}
								<button type="button" class="ledger-retry" @click="emit('retry')">
									Retry
								</button>
							</div>
							<template v-else>{{ notice }}</template>
						</td>
					</tr>
					<tr v-for="row in rows" :key="row.id">
						<td class="invoice-column">
							<span class="invoice-reference">{{ row.voucher_no }}</span>
							<small>{{
								[row.settlement, row.term].filter(Boolean).join(" · ")
							}}</small>
						</td>
						<td class="party-column">{{ row.party_name || row.party }}</td>
						<td class="date-column">{{ dueLabel(row.due_date, anchor) }}</td>
						<td class="original-column">
							<span class="mobile-label">Invoice </span>{{ money(row.original) }}
						</td>
						<td class="settled-column">
							<span class="mobile-label">Settled </span>{{ money(row.settled) }}
						</td>
						<td class="remaining-column">
							<span class="mobile-label">Remaining</span
							><strong>{{ money(row.remaining) }}</strong>
						</td>
					</tr>
				</tbody>
			</table>
		</div>
		<div class="modal-foot">
			<p>Settlement can include payments, credits and discounts.</p>
			<div class="modal-pager">
				<span v-if="records !== null">{{ records }} records</span>
				<button
					type="button"
					class="pcal-btn page-previous"
					:disabled="modal.loading || modal.page <= 1"
					@click="emit('page', modal.page - 1)"
				>
					Previous
				</button>
				<span>Page {{ modal.page }}</span>
				<button
					type="button"
					class="pcal-btn page-next"
					:disabled="modal.loading || !modal.has_more"
					@click="emit('page', modal.page + 1)"
				>
					Next
				</button>
			</div>
		</div>
	</PcalModal>
</template>
<script setup>
import { computed } from "vue";
import { snapshotMoney } from "@/lib/paymentCalendar/format";
import PcalModal from "./PcalModal.vue";
import LedgerTabs from "./LedgerTabs.vue";
import { explain } from "./messages";
import { dialogContext, dueLabel, scopeTitle, sideTabs } from "./ledgerCopy";
import "./tokens.css";
import "./InvoiceDetailsDialog.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	// usePaymentLedger().modal: open, scope, side, date, page, rows, total, total_remaining, ...
	modal: { type: Object, required: true },
	sortKey: { type: String, required: true },
	sortDirection: { type: String, required: true },
});
// side, sort (a key) and page (a number) go to usePaymentLedger; the page owns the rest.
const emit = defineEmits(["close", "side", "sort", "page", "retry"]);

// The template's sort keys (data-sort, the select values) and the API's: only the date differs.
const API_KEY = { remaining: "remaining", date: "due_date", party: "party" };
const SORT_OPTIONS = [
	["remaining", "Remaining balance"],
	["date", "Due date"],
	["party", "Party name"],
];
const COLUMNS = [
	{ title: "Invoice / term" },
	{ title: "Party", sort: "party" },
	{ title: "Due date", sort: "date" },
	{ title: "Invoice amount" },
	{ title: "Settled" },
	{ title: "Remaining", sort: "remaining" },
];

const anchor = computed(() => props.snapshot.scope.anchor);
const title = computed(() =>
	scopeTitle(props.modal.scope, { date: props.modal.date, anchor: anchor.value })
);
const context = computed(() => dialogContext(props.modal.scope, props.snapshot.generated_at));
const tabs = computed(() => sideTabs(props.snapshot, props.modal.scope, props.modal.date));
const records = computed(() => (Number.isInteger(props.modal.total) ? props.modal.total : null));
const money = (value) => snapshotMoney(props.snapshot).exact(value);
const summary = computed(() => {
	if (records.value === null) return "";
	const total = props.modal.total_remaining;
	return `${records.value} records · ${total === null ? "Unavailable" : money(total)}`;
});
// Empty, loading, failed and unknown read as one message row; rows never mix with it.
const notice = computed(() => {
	const { loading, error, unavailable, rows } = props.modal;
	if (loading) return "Loading records…";
	if (error) return explain(error.code);
	if (unavailable) return "Invoice data is unavailable for this month.";
	return rows.length ? "" : "No records for this selection.";
});
const rows = computed(() => (notice.value ? [] : props.modal.rows));

const templateKey = computed(() =>
	Object.keys(API_KEY).find((key) => API_KEY[key] === props.sortKey)
);
const chooseSort = (key) => emit("sort", API_KEY[key]);
const ariaSort = (key) =>
	templateKey.value !== key
		? "none"
		: props.sortDirection === "asc"
		? "ascending"
		: "descending";
const headAttrs = (column) => (column.sort ? { "aria-sort": ariaSort(column.sort) } : {});
const arrow = (key) =>
	templateKey.value !== key ? "" : props.sortDirection === "asc" ? " ↑" : " ↓";
</script>
