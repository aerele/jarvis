<template>
	<section
		class="pcal-panel detail-panel"
		:aria-busy="loading"
		:aria-label="title || 'Selected date details'"
	>
		<div class="pcal-panel-heading">
			<div>
				<h2 aria-live="polite">{{ title || dateLabel(selected, true) }}</h2>
				<p>
					{{
						title
							? "All matching records in this snapshot"
							: "Remaining balances as of this snapshot"
					}}
				</p>
			</div>
			<span v-if="data" class="pcal-chip"
				>{{ data.total }} {{ data.total === 1 ? "row" : "rows" }}</span
			>
		</div>
		<div v-if="!title" class="pcal-tabs" role="group" aria-label="Detail view">
			<button
				v-for="tab in tabs"
				:key="tab.value"
				:aria-pressed="view === tab.value"
				@click="$emit('view', tab.value)"
			>
				{{ tab.label }}
			</button>
		</div>
		<p v-if="unavailable" class="pcal-empty">
			{{ view === "customers" ? "Customer collections" : "Supplier dues" }} unavailable for
			this scope.
		</p>
		<p v-else-if="loading" class="pcal-empty" role="status">Loading details…</p>
		<template v-else-if="data">
			<template v-if="view !== 'terms'">
				<div class="pcal-table-wrap overflow-x-auto">
					<table class="pcal-table">
						<thead>
							<tr>
								<th>{{ view === "customers" ? "Customer" : "Payee" }}</th>
								<th class="numeric">Documents</th>
								<th class="numeric">Rows</th>
								<th class="numeric">Remaining</th>
							</tr>
						</thead>
						<tbody>
							<tr
								v-for="party in data.parties"
								:key="party.party_type + ':' + party.party"
							>
								<td>
									<strong>{{ party.party_name || party.party }}</strong
									><small>{{ party.party_type }}</small>
								</td>
								<td class="numeric">{{ party.invoices }}</td>
								<td class="numeric">{{ party.terms }}</td>
								<td class="numeric money">{{ money(party.remaining) }}</td>
							</tr>
						</tbody>
					</table>
				</div>
				<p v-if="!data.party_total" class="pcal-empty">
					No {{ view === "customers" ? "customer collections" : "supplier dues" }} on
					this date.
				</p>
				<div v-if="data.party_total" class="pcal-pagination">
					<span
						>{{ pageRange(data.party_page, data.party_total) }} of
						{{ data.party_total }}
						{{ view === "customers" ? "customers" : "payees" }}</span
					>
					<div>
						<button
							class="pcal-button"
							:aria-label="`Previous ${
								view === 'customers' ? 'customers' : 'payees'
							} page`"
							:disabled="data.party_page <= 1"
							@click="$emit('page', 'parties', data.party_page - 1)"
						>
							Previous</button
						><button
							class="pcal-button"
							:aria-label="`Next ${
								view === 'customers' ? 'customers' : 'payees'
							} page`"
							:disabled="data.party_page * data.page_size >= data.party_total"
							@click="$emit('page', 'parties', data.party_page + 1)"
						>
							Next
						</button>
					</div>
				</div>
			</template>
			<template v-if="view !== 'payees'">
				<h3 v-if="view === 'customers'" class="pcal-subheading">
					Customer invoice terms & other entries
				</h3>
				<div class="pcal-table-wrap overflow-x-auto">
					<table class="pcal-table terms-table">
						<thead>
							<tr>
								<th>Document / term</th>
								<th>Party</th>
								<th>Due date</th>
								<th class="numeric">Remaining</th>
								<th>Settlement</th>
							</tr>
						</thead>
						<tbody>
							<template v-for="row in data.rows" :key="row.id"
								><tr>
									<td>
										<a
											v-if="safeLink(row.source_link)"
											:href="row.source_link"
											target="_blank"
											rel="noopener noreferrer"
											>{{ row.voucher_no }}</a
										><strong v-else>{{ row.voucher_no }}</strong
										><small
											>{{ row.voucher_type
											}}{{ row.term ? " · " + row.term : "" }}</small
										>
										<small v-if="row.allocation_basis === 'invoice'">
											Invoice-level balance · payment-term breakdown
											unavailable.
											{{
												row.allocation_reason === "withholding"
													? "Tax withholding is included in the ledger balance."
													: "A foreign-currency advance is included in the ledger balance."
											}}
										</small>
									</td>
									<td>
										{{ row.party_name || row.party
										}}<small>{{ row.party_type }}</small>
									</td>
									<td>
										{{
											row.due_date ? dateLabel(row.due_date) : "No due date"
										}}
									</td>
									<td class="numeric money">{{ money(row.remaining) }}</td>
									<td>{{ row.settlement }}</td>
								</tr>
								<tr class="allocation-row">
									<td colspan="5">
										<dl>
											<div v-for="field in allocations" :key="field.key">
												<dt>{{ field.label }}</dt>
												<dd>{{ money(row[field.key]) }}</dd>
											</div>
										</dl>
									</td>
								</tr></template
							>
						</tbody>
					</table>
				</div>
				<p v-if="!data.total" class="pcal-empty">No records for this selection.</p>
				<div v-if="data.total" class="pcal-pagination">
					<span>{{ pageRange(data.page, data.total) }} of {{ data.total }} rows</span>
					<div>
						<button
							class="pcal-button"
							aria-label="Previous records page"
							:disabled="data.page <= 1"
							@click="$emit('page', 'rows', data.page - 1)"
						>
							Previous</button
						><button
							class="pcal-button"
							aria-label="Next records page"
							:disabled="!data.has_more"
							@click="$emit('page', 'rows', data.page + 1)"
						>
							Next
						</button>
					</div>
				</div>
			</template>
			<p class="pcal-footnote">
				Rows can include payment terms and other accounting entries. Settlement includes
				payments, credits and discounts; it does not confirm bank clearance.
			</p>
		</template>
	</section>
</template>
<script setup>
import { computed } from "vue";
import { exactMoney } from "@/lib/paymentCalendar/format";
import { dateLabel } from "@/lib/paymentCalendar/dates";
const props = defineProps({
	snapshot: { type: Object, required: true },
	data: { type: Object, default: null },
	view: { type: String, default: "payees" },
	selected: { type: String, default: "" },
	loading: Boolean,
	title: { type: String, default: "" },
	bucketSide: { type: String, default: "" },
});
defineEmits(["view", "page"]);
const tabs = [
	{ value: "payees", label: "Payees" },
	{ value: "terms", label: "Invoice terms" },
	{ value: "customers", label: "Customer collections" },
];
const allocations = [
	{ key: "original", label: "Original" },
	{ key: "paid", label: "Paid" },
	{ key: "credit", label: "Credit" },
	{ key: "discount", label: "Discount" },
];
const unavailable = computed(
	() =>
		props.snapshot.unavailable?.[
			props.bucketSide || (props.view === "customers" ? "Receivable" : "Payable")
		],
);
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
const safeLink = (value) =>
	typeof value === "string" &&
	/^\/app\/[a-z0-9-]+\/[^\s\\]+$/i.test(value) &&
	!/[<>"']/.test(value);
function pageRange(page, total) {
	return `${(page - 1) * props.data.page_size + 1}–${Math.min(
		page * props.data.page_size,
		total,
	)}`;
}
</script>
