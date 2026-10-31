<template>
	<section class="pcal-panel reconciliation-panel" aria-label="Outstanding balances">
		<div class="reconciliation-summary">
			<div>
				<h3>Payable now · {{ dateLabel(snapshot.scope.anchor) }}</h3>
				<strong>{{ money(snapshot.projection.due_now.total) }}</strong>
				<p>
					Today {{ money(snapshot.projection.due_now.scheduled) }}<br />
					Overdue {{ money(snapshot.projection.due_now.overdue) }}<br />
					Undated {{ money(snapshot.projection.due_now.undated) }}
				</p>
			</div>
			<div>
				<h3>Overdue & undated receivables</h3>
				<p>
					Overdue {{ money(snapshot.projection.excluded_collections.overdue) }}<br />No
					due date {{ money(snapshot.projection.excluded_collections.undated) }}
				</p>
			</div>
		</div>
		<details class="reconciliation-details">
			<summary>Balance details</summary>
			<div class="pcal-table-wrap overflow-x-auto">
				<table class="pcal-table">
					<thead>
						<tr>
							<th>Category</th>
							<th class="numeric">Suppliers</th>
							<th class="numeric">Customers</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="item in buckets" :key="item.key">
							<th scope="row">{{ item.label }}</th>
							<td v-for="side in sides" :key="side" class="numeric money">
								<button
									v-if="item.drill && snapshot.projection.reconciliation[side]"
									class="pcal-text-button"
									:aria-label="`${
										side === 'Payable' ? 'Supplier' : 'Customer'
									} ${item.label} records`"
									@click="$emit('bucket', side, item.key)"
								>
									{{
										money(snapshot.projection.reconciliation[side]?.[item.key])
									}}</button
								><span v-else>{{
									money(snapshot.projection.reconciliation[side]?.[item.key])
								}}</span>
							</td>
						</tr>
						<tr v-if="hasExchangeDifference">
							<th scope="row">Exchange differences (excluded)</th>
							<td v-for="side in sides" :key="side" class="numeric money">
								{{ money(exchangeDifference(side)) }}
							</td>
						</tr>
					</tbody>
				</table>
			</div>
		</details>
	</section>
</template>
<script setup>
import { computed } from "vue";
import { exactMoney } from "@/lib/paymentCalendar/format";
import { dateLabel } from "@/lib/paymentCalendar/dates";
const props = defineProps({ snapshot: { type: Object, required: true } });
defineEmits(["bucket"]);
const sides = ["Payable", "Receivable"];
const buckets = [
	{ key: "canonical_total", label: "Total outstanding" },
	{ key: "current_future", label: "Due today & later" },
	{ key: "overdue", label: "Overdue", drill: true },
	{ key: "undated", label: "No due date", drill: true },
	{ key: "outside_horizon", label: "Outside forecast", drill: true },
	{ key: "adjustments", label: "Adjustments", drill: true },
];
const hasExchangeDifference = computed(() =>
	sides.some(
		(side) => props.snapshot.projection.source_notes?.[side]?.currency_residue_excluded?.count
	)
);
const exchangeDifference = (side) =>
	props.snapshot.projection.reconciliation[side] == null
		? null
		: props.snapshot.projection.source_notes?.[side]?.currency_residue_excluded?.amount ??
		  null;
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
</script>
