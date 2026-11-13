<template>
	<div
		v-if="open"
		id="accounts-menu"
		class="menu account-picker"
		role="dialog"
		aria-label="Choose bank and cash accounts"
	>
		<div class="account-picker-head">
			<strong>Bank & cash accounts</strong><span>Choose up to {{ LIMIT }} accounts</span>
		</div>
		<input
			id="accounts-search"
			ref="searchInput"
			v-model="query"
			type="search"
			aria-label="Search bank and cash accounts"
			placeholder="Search bank, account or last digits…"
			autocomplete="off"
		/>
		<div class="account-picker-tools">
			<span id="accounts-count">{{ countLine }}</span>
			<div>
				<button
					id="accounts-select-matching"
					type="button"
					class="text-btn"
					:disabled="!canSelectMatching"
					:title="overLimit ? OVER_LIMIT_TITLE : ''"
					@click="selectMatching"
				>
					Select matching
				</button>
				<button
					id="accounts-clear"
					type="button"
					class="text-btn"
					:disabled="!draft.size"
					@click="draft = new Set()"
				>
					Clear
				</button>
			</div>
		</div>
		<p v-if="overLimit" id="accounts-limit-note" class="account-limit-note">
			{{ OVER_LIMIT_TITLE }}
		</p>
		<div id="accounts-options" class="account-options">
			<label v-for="account in matching" :key="account.name" class="account-option">
				<input
					type="checkbox"
					:aria-label="accountLabel(account)"
					:checked="draft.has(account.name)"
					:disabled="!draft.has(account.name) && draft.size >= LIMIT"
					@change="toggle(account.name, $event.target.checked)"
				/>
				<span
					><strong>{{ accountLabel(account) }}</strong
					><small>{{ accountDetail(account) }}</small></span
				>
			</label>
			<p v-if="!matching.length" class="schedule-empty">
				{{
					catalog.length
						? "No matching accounts."
						: "This company has no bank or cash accounts you can choose."
				}}
			</p>
		</div>
		<div class="menu-actions">
			<button id="accounts-cancel" type="button" class="btn" @click="emit('close')">
				Cancel
			</button>
			<button id="accounts-apply" type="button" class="btn primary" @click="apply">
				Apply accounts
			</button>
		</div>
	</div>
</template>
<script setup>
import { computed, nextTick, ref, watch } from "vue";
import { accountDetail, accountLabel, matchingAccounts } from "./accountSearch";
import { OVERLAY_PRIORITY, useOverlay } from "./overlayStack";

// Keep in sync with the 50-account cap in setAccounts (usePaymentCalendar.js) and the server.
const LIMIT = 50;
const OVER_LIMIT_TITLE = "Narrow the search to stay within the 50-account selection limit.";

const props = defineProps({
	open: { type: Boolean, default: false },
	// get_capabilities().accounts
	catalog: { type: Array, default: () => [] },
	// the active account names
	selected: { type: Array, default: () => [] },
});
const emit = defineEmits(["apply", "close"]);

const draft = ref(new Set()),
	query = ref(""),
	searchInput = ref(null);

const inCatalog = (name) => props.catalog.some((account) => account.name === name);
const matching = computed(() => matchingAccounts(props.catalog, query.value));
const unselected = computed(() =>
	matching.value.filter((account) => !draft.value.has(account.name))
);
const overLimit = computed(() => unselected.value.length > LIMIT - draft.value.size);
const canSelectMatching = computed(() => unselected.value.length > 0 && !overLimit.value);
const countLine = computed(
	() =>
		`${draft.value.size} of ${LIMIT} selected · ${matching.value.length} of ${props.catalog.length} accounts`
);

useOverlay(
	() => props.open,
	OVERLAY_PRIORITY.picker,
	() => emit("close")
);

// Opening copies the active set into the draft; closing discards it.
watch(
	() => props.open,
	(open) => {
		if (!open) return;
		draft.value = new Set(props.selected.filter((name) => inCatalog(name)));
		query.value = "";
		void nextTick(() => searchInput.value?.focus());
	},
	{ immediate: true }
);

function toggle(name, on) {
	const next = new Set(draft.value);
	if (on) next.add(name);
	else next.delete(name);
	draft.value = next;
}
function selectMatching() {
	draft.value = new Set([...draft.value, ...unselected.value.map((account) => account.name)]);
}
function apply() {
	const names = props.catalog.map((account) => account.name);
	const applied = names.filter((name) => draft.value.has(name));
	const current = names.filter((name) => props.selected.includes(name));
	// Nothing changed: close like Cancel, so no snapshot is requested for the same set.
	if (applied.length === current.length && applied.every((name, at) => name === current[at]))
		return emit("close");
	emit("apply", applied);
}
</script>
