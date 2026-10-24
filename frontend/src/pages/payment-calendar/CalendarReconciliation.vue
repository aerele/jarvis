<template>
	<section class="pcal-panel reconciliation-panel">
		<div class="pcal-panel-heading">
			<div>
				<h2>What the forecast includes</h2>
				<p>Every bucket reconciles to the current ERPNext outstanding balance.</p>
			</div>
		</div>
		<div class="reconciliation-summary">
			<div>
				<h3>Payable now · {{ dateLabel(snapshot.scope.anchor) }}</h3>
				<strong>{{ money(snapshot.projection.due_now.total) }}</strong>
				<p>
					Scheduled today {{ money(snapshot.projection.due_now.scheduled) }}<br />Plus
					overdue {{ money(snapshot.projection.due_now.overdue) }} and undated
					{{ money(snapshot.projection.due_now.undated) }}, charged once today.
				</p>
			</div>
			<div>
				<h3>Customer money excluded from expected cash</h3>
				<p>
					Overdue {{ money(snapshot.projection.excluded_collections.overdue) }}<br />No
					due date {{ money(snapshot.projection.excluded_collections.undated) }}
				</p>
				<p>
					Neither is assumed to arrive today. Future dated collections assume on-time
					payment.
				</p>
			</div>
		</div>
		<details class="reconciliation-details">
			<summary>Reconcile all outstanding balances</summary>
			<div class="pcal-table-wrap overflow-x-auto">
				<table class="pcal-table">
					<thead>
						<tr>
							<th>Bucket</th>
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
					</tbody>
				</table>
			</div>
			<p class="pcal-footnote">
				Canonical outstanding = current / future + overdue + undated + outside forecast +
				signed adjustments. Negative credits and advances remain signed here; they are not
				assumed refunds or cash collections.
			</p>
		</details>
	</section>
</template>
<script setup>
import { exactMoney } from "@/lib/paymentCalendar/format";
import { dateLabel } from "@/lib/paymentCalendar/dates";
const props = defineProps({ snapshot: { type: Object, required: true } });
defineEmits(["bucket"]);
const sides = ["Payable", "Receivable"];
const buckets = [
	{ key: "canonical_total", label: "Canonical outstanding" },
	{ key: "current_future", label: "Current / future in forecast" },
	{ key: "overdue", label: "Overdue", drill: true },
	{ key: "undated", label: "No due date", drill: true },
	{ key: "outside_horizon", label: "Outside forecast", drill: true },
	{ key: "adjustments", label: "Signed adjustments", drill: true },
];
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
</script>
