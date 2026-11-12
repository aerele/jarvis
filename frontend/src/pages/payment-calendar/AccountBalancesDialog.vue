<template>
	<PcalModal
		:model-value="modelValue"
		title="Bank & cash account balances"
		close-label="Close account balances"
		close-id="close-aux"
		size="sm"
		layer="aux"
		@update:model-value="emit('update:modelValue', $event)"
	>
		<div class="aux-body">
			<p v-if="asOf" class="account-balance-context">Book balances as of {{ asOf }}</p>
			<div class="account-balance-scope">
				<span>{{ scopeLine }}</span>
				<button id="account-balance-toggle" type="button" class="text-btn" @click="toggle">
					{{ showAll ? "Show selected accounts" : "Show all accounts" }}
				</button>
			</div>
			<div class="account-balance-list" aria-live="polite">
				<p v-if="loading || refreshing" class="schedule-empty">
					Loading account balances…
				</p>
				<template v-else-if="failed">
					<p class="schedule-empty">{{ explainBalances(balances.errorCode.value) }}</p>
					<button
						v-if="balancesRetryable(balances.errorCode.value)"
						id="account-balances-retry"
						type="button"
						class="text-btn"
						@click="balances.load(company)"
					>
						Try again
					</button>
				</template>
				<p v-else-if="notice" class="schedule-empty">{{ notice }}</p>
				<div v-for="row in rows" v-else :key="row.account" class="account-balance-row">
					<span
						><strong>{{ row.name }}</strong
						><small>{{ row.detail }}</small></span
					><b>{{ row.balance }}</b>
				</div>
			</div>
			<div v-if="total" class="account-balance-total">
				<span>{{ total.label }}</span
				><strong>{{ total.amount }}</strong>
			</div>
			<p class="demo-note">
				The forecast uses selected accounts only. These are book balances in
				{{ snapshot.currency }}.
			</p>
		</div>
	</PcalModal>
</template>
<script setup>
import { computed, ref, watch } from "vue";
import { useAccountBalances } from "@/composables/useAccountBalances";
import { snapshotStamp } from "@/lib/paymentCalendar/dates";
import { exactMoney } from "@/lib/paymentCalendar/format";
import PcalModal from "./PcalModal.vue";
import { balancesRetryable, explainBalances } from "./messages";
import "./AccountBalancesDialog.css";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	snapshot: { type: Object, required: true },
	company: { type: String, default: "" },
	timezone: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue"]);

const balances = useAccountBalances();
const showAll = ref(false);

const loading = computed(() => showAll.value && balances.state.value === "loading");
// The core's loading frame has no snapshot id: its balances are not in yet.
const refreshing = computed(() => !showAll.value && !props.snapshot.snapshot_id);
const failed = computed(() => showAll.value && balances.state.value === "failed");
const selectedNames = computed(() => props.snapshot.scope?.accounts || []);
// Selected view: the snapshot's own bank block. All view: the finished job.
const source = computed(() => (showAll.value ? balances.result.value : props.snapshot.bank));
const rows = computed(() =>
	(source.value?.accounts || []).map((account) => ({
		account: account.account,
		name: account.account_name || account.account,
		detail: [
			account.account_type,
			account.bank,
			account.reference,
			showAll.value && selectedNames.value.includes(account.account) ? "Selected" : "",
		]
			.filter(Boolean)
			.join(" · "),
		balance: amount(account.balance),
	}))
);
const asOf = computed(() => {
	const read = showAll.value ? balances.result.value?.generated_at : props.snapshot.generated_at;
	return read ? snapshotStamp(read, props.timezone) : "";
});
const scopeLine = computed(() => {
	if (!showAll.value) return `Selected accounts · ${rows.value.length}`;
	return source.value ? `All accounts · ${rows.value.length}` : "All accounts";
});
// Why the list is empty when it is not loading or failed.
const notice = computed(() => {
	if (rows.value.length) return "";
	if (showAll.value)
		return balances.state.value === "ready" ? "No bank or cash accounts you can view." : "";
	if (!selectedNames.value.length)
		return "No accounts selected. Select accounts to include book cash in the forecast.";
	return props.snapshot.bank ? "" : "Account balances are not available for this snapshot.";
});
const total = computed(() => {
	if (loading.value || failed.value || refreshing.value || (showAll.value && notice.value))
		return null;
	return showAll.value
		? { label: "Total shown · all accounts", amount: amount(source.value?.total) }
		: { label: "Book cash now · selected accounts", amount: amount(source.value?.total) };
});

// The server's currency and precision for each source; "Unavailable" for a missing value.
function amount(value) {
	if (value == null || !source.value) return "Unavailable";
	const text = exactMoney(value, source.value.currency, source.value.precision);
	return text === "—" ? "Unavailable" : text;
}

function toggle() {
	showAll.value = !showAll.value;
	if (showAll.value) void balances.load(props.company);
	else balances.reset();
}

// Every open starts on the selected view; closing stops a running job read.
watch(
	() => props.modelValue,
	() => {
		showAll.value = false;
		balances.reset();
	}
);
</script>
