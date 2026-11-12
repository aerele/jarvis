<template>
	<section class="filters" aria-label="Calendar filters">
		<CompanyMenu
			:open="menu === 'company'"
			:companies="companies"
			:model-value="company"
			:disabled="companyDisabled"
			@update:open="setMenu('company', $event)"
			@select="chooseCompany"
		/>
		<div ref="accountsCell" class="filter accounts-filter">
			<span class="field-label">Bank & cash accounts</span>
			<div class="account-summary">
				<div
					id="account-chips"
					class="account-chips"
					role="group"
					aria-label="Selected bank and cash accounts"
				>
					<button
						v-for="account in chips"
						:key="account.name"
						type="button"
						class="account-chip active"
						:aria-label="`Remove ${accountLabel(account)}`"
						@click="remove(account.name)"
					>
						{{ accountLabel(account) }}<span aria-hidden="true">×</span>
					</button>
					<button
						v-if="overflow"
						id="accounts-overflow"
						type="button"
						class="account-chip account-overflow"
						@click="setMenu('accounts', true)"
					>
						+{{ overflow }} selected
					</button>
					<span v-if="!accounts.length" class="no-accounts">No accounts selected</span>
				</div>
				<button
					id="accounts-trigger"
					ref="trigger"
					type="button"
					class="account-manage"
					aria-haspopup="dialog"
					:aria-expanded="String(menu === 'accounts')"
					:disabled="!caps"
					@click="setMenu('accounts', menu !== 'accounts')"
				>
					Choose accounts
				</button>
			</div>
			<AccountPicker
				:open="menu === 'accounts'"
				:catalog="catalog"
				:selected="accounts"
				@apply="apply"
				@close="closePicker"
			/>
		</div>
		<FloorField
			:settings="caps?.settings || null"
			:can-configure="!!caps?.can_configure"
			:saving="floorSaving"
			:error="floorError"
			:save="saveFloor"
		/>
	</section>
</template>
<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import AccountPicker from "./AccountPicker.vue";
import CompanyMenu from "./CompanyMenu.vue";
import FloorField from "./FloorField.vue";
import { accountLabel } from "./accountSearch";
import "./tokens.css";
import "./CalendarFilterBar.css";

const props = defineProps({
	companies: { type: Array, default: () => [] },
	company: { type: String, default: "" },
	// get_capabilities(company): { accounts, settings, can_configure }
	caps: { type: Object, default: null },
	// the active account names
	accounts: { type: Array, default: () => [] },
	companyDisabled: { type: Boolean, default: false },
	floorSaving: { type: Boolean, default: false },
	floorError: { type: Object, default: null },
	saveFloor: { type: Function, required: true },
});
const emit = defineEmits(["choose-company", "set-accounts"]);

const CHIP_LIMIT = 2;
const menu = ref(null),
	accountsCell = ref(null),
	trigger = ref(null);

const catalog = computed(() => props.caps?.accounts || []);
const selectedAccounts = computed(() =>
	catalog.value.filter((account) => props.accounts.includes(account.name))
);
const chips = computed(() => selectedAccounts.value.slice(0, CHIP_LIMIT));
const overflow = computed(() => Math.max(0, selectedAccounts.value.length - CHIP_LIMIT));

// One menu at a time: opening one closes the other.
function setMenu(name, open) {
	if (open) menu.value = name;
	else if (menu.value === name) menu.value = null;
}

function chooseCompany(name) {
	if (name !== props.company) emit("choose-company", name);
}
function remove(name) {
	emit(
		"set-accounts",
		props.accounts.filter((item) => item !== name)
	);
	// The chip is gone; keep the keyboard where the person was working.
	void nextTick(() => trigger.value?.focus());
}
function closePicker() {
	setMenu("accounts", false);
	void nextTick(() => trigger.value?.focus());
}
function apply(names) {
	emit("set-accounts", names);
	closePicker();
}

// The accounts picker closes on a pointerdown anywhere outside its filter cell.
function closeOutside(event) {
	if (!accountsCell.value?.contains(event.target)) setMenu("accounts", false);
}
watch(
	() => menu.value === "accounts",
	(open) => {
		if (open) document.addEventListener("pointerdown", closeOutside);
		else document.removeEventListener("pointerdown", closeOutside);
	}
);
onBeforeUnmount(() => document.removeEventListener("pointerdown", closeOutside));
</script>
