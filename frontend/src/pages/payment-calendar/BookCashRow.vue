<template>
	<div class="headline-context">
		<span
			>Book cash now
			<button id="header-book-cash" type="button" :disabled="loading" @click="emit('open')">
				{{ loading ? "Loading values…" : bookCash }}
			</button></span
		>
		<button id="account-balances" type="button" :disabled="loading" @click="emit('open')">
			View account balances →
		</button>
		<span v-if="snapshot.snapshot_id" class="context-time"
			>Snapshot · {{ snapshotStamp(snapshot.generated_at, timezone) }}</span
		>
	</div>
</template>
<script setup>
import { computed } from "vue";
import { snapshotStamp } from "@/lib/paymentCalendar/dates";
import { signedMoney } from "@/lib/paymentCalendar/format";
import "./tokens.css";
import "./BookCashRow.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	timezone: { type: String, default: "" },
});
const emit = defineEmits(["open"]);

// The loading frame has identities only: say so, like the headline cards.
const loading = computed(() => !props.snapshot.snapshot_id);
const bookCash = computed(() =>
	signedMoney(
		props.snapshot.projection?.book_cash,
		props.snapshot.currency,
		props.snapshot.projection?.precision
	)
);
</script>
