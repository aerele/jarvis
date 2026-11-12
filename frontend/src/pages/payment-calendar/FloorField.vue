<template>
	<div class="filter floor-filter">
		<label class="field-label" for="floor-input">Minimum cash balance</label>
		<div class="currency-field">
			<span aria-hidden="true">{{ symbol }}</span>
			<input
				id="floor-input"
				v-model="text"
				inputmode="decimal"
				autocomplete="off"
				placeholder="—"
				aria-describedby="floor-help floor-error"
				:aria-invalid="message ? 'true' : undefined"
				:title="readOnlyReason"
				:readonly="!canConfigure || saving"
				:disabled="!settings"
				@input="edited"
				@keydown.enter.prevent="submit"
				@keydown.esc.stop="revert"
			/>
			<button
				v-if="canConfigure"
				id="floor-save"
				type="button"
				class="floor-save"
				:hidden="!dirty"
				:disabled="saving"
				@click="submit"
			>
				{{ saving ? "Saving…" : "Save" }}
			</button>
		</div>
		<small id="floor-help">{{
			canConfigure ? "Shared minimum · Save to apply" : "Shared minimum · View only"
		}}</small>
		<span id="floor-error" role="alert">{{ message }}</span>
	</div>
</template>
<script setup>
import { computed, ref, watch } from "vue";
import { exactMoney } from "@/lib/paymentCalendar/format";
import { formatFloor, parseFloor, sameFloor } from "./floorInput";
import { explain } from "./messages";

const props = defineProps({
	settings: { type: Object, default: null },
	canConfigure: { type: Boolean, default: false },
	saving: { type: Boolean, default: false },
	// the core's floorError: { code, state, retryable } or null
	error: { type: Object, default: null },
	// (value: string) => Promise<{ ok }>; the core's saveFloor
	save: { type: Function, required: true },
});

const saved = computed(() => (props.settings?.configured ? props.settings.safety_floor : null));
const text = ref(formatFloor(saved.value));
const localError = ref(""),
	serverErrorHidden = ref(false);

const dirty = computed(() => props.canConfigure && !sameFloor(text.value, saved.value));
const message = computed(
	() =>
		localError.value ||
		(props.error && !serverErrorHidden.value ? explain(props.error.code) : "")
);
// Why the field cannot be edited; nothing while the person can edit or access is gone.
const readOnlyReason = computed(() =>
	props.settings && !props.canConfigure
		? "You need permission to change the shared minimum cash balance."
		: undefined
);
// "₹0" without its digit; empty when the company's currency is unknown.
const symbol = computed(() => {
	const sample = exactMoney("0", props.settings?.currency, 0);
	return sample === "—" ? "" : sample.slice(0, -1);
});

// A saved change replaces the text only when the person has not typed something else.
watch(saved, (next, previous) => {
	if (sameFloor(text.value, previous) || sameFloor(text.value, next))
		text.value = formatFloor(next);
});
watch(
	() => props.error,
	() => (serverErrorHidden.value = false)
);

function edited() {
	localError.value = "";
	serverErrorHidden.value = true;
}

async function submit() {
	if (!dirty.value || props.saving) return;
	const parsed = parseFloor(text.value);
	localError.value = parsed.error || "";
	if (parsed.error) return;
	await props.save(parsed.value);
}

function revert() {
	text.value = formatFloor(saved.value);
	localError.value = "";
	serverErrorHidden.value = true;
}
</script>
