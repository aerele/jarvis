<template>
	<div class="pcal-root flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
		<LayoutHeader
			><template #left-header><Breadcrumbs :items="breadcrumbs" /></template
		></LayoutHeader>
		<main class="pcal-main" aria-labelledby="pcal-title">
			<div class="pcal-title-row">
				<div>
					<p class="pcal-eyebrow">CASH PLANNING</p>
					<h1 id="pcal-title">Payment Calendar</h1>
					<p class="pcal-subtitle">
						Know what is due. See the days that need attention.
					</p>
				</div>
				<button
					class="pcal-button pcal-primary"
					:disabled="!model.company || busy"
					@click="model.refresh()"
				>
					<FeatherIcon name="refresh-cw" class="size-4" />Refresh
				</button>
			</div>
			<section class="pcal-toolbar" aria-label="Calendar filters">
				<label class="pcal-field"
					>Company<select
						:value="model.company"
						:disabled="model.phase === 'loading'"
						@change="model.chooseCompany($event.target.value)"
					>
						<option value="">Select a company</option>
						<option
							v-for="item in model.companies"
							:key="item.name"
							:value="item.name"
						>
							{{ item.name }}
						</option>
					</select></label
				>
				<div class="pcal-field">
					<span>Bank & cash accounts</span
					><button
						class="pcal-select-button"
						:disabled="!model.caps"
						@click="openAccounts"
					>
						<span>{{
							model.accounts.length
								? `${model.accounts.length} selected`
								: "Select accounts"
						}}</span
						><FeatherIcon name="chevron-down" class="size-4" />
					</button>
				</div>
				<div class="pcal-field minimum-field">
					<span>Minimum cash balance</span>
					<div class="minimum-value">
						<strong>{{ configuredFloor }}</strong
						><button
							v-if="model.caps?.can_configure"
							class="pcal-text-button"
							@click="openFloor"
						>
							{{ model.caps.settings?.configured ? "Edit" : "Set minimum" }}
						</button>
					</div>
				</div>
			</section>
			<p v-if="model.preferenceError" class="pcal-inline-error" role="status">
				Your selection could not be saved.
				<button class="pcal-text-button" @click="model.saveSelection()">
					Retry saving
				</button>
			</p>
			<p v-if="model.notice" class="pcal-notice" role="status">{{ model.notice }}</p>
			<template v-if="model.snapshot">
				<div class="pcal-asof">
					<span
						><i class="snapshot-dot"></i>As of
						{{ snapshotTime(model.snapshot.generated_at, timezone()) }} ({{
							timezone() || "UTC"
						}})</span
					><span>{{ model.snapshot.currency }} · Expires after 5 minutes</span>
				</div>
				<div
					v-if="!model.snapshot.projection.forecast_available"
					class="pcal-warning"
					role="status"
				>
					<strong>Cash forecast unavailable</strong>
					<p v-for="reason in model.snapshot.projection.scope_reasons" :key="reason">
						{{ explain(reason) }}
					</p>
					<p v-if="model.snapshot.bank_code">{{ explain(model.snapshot.bank_code) }}</p>
				</div>
				<p v-if="!model.accounts.length" class="pcal-notice">
					Choose bank and cash accounts to include book cash in the forecast.
				</p>
				<CalendarKpis
					:snapshot="model.snapshot"
					:day="model.day"
					:selected="model.selectedDate"
				/>
				<p class="pcal-assumptions">
					Customer collections assume on-time payment. Overdue and undated supplier dues
					are charged once today, in addition to today's scheduled dues.
				</p>
				<div class="pcal-section-bar">
					<div>
						<h2>Your payment schedule</h2>
						<p>Click any date to update the figures and details below.</p>
					</div>
					<div class="pcal-segment" role="group" aria-label="Calendar display">
						<button :aria-pressed="mode === 'month'" @click="mode = 'month'">
							Month</button
						><button :aria-pressed="mode === 'list'" @click="mode = 'list'">
							List
						</button>
					</div>
				</div>
				<div class="pcal-calendar-layout">
					<CalendarGrid
						:snapshot="model.snapshot"
						:selected="model.selectedDate"
						:month="model.month"
						:mode="mode"
						@select="model.selectDate"
						@browse="model.browseMonth"
						@today="model.goToday"
					/><UpcomingPayments
						:snapshot="model.snapshot"
						:selected="model.selectedDate"
						:rows="model.upcoming"
						:loading="model.upcomingLoading"
						@select="model.selectDate"
					/>
				</div>
				<CalendarDetails
					:snapshot="model.snapshot"
					:selected="model.selectedDate"
					:data="model.detail"
					:loading="model.detailLoading"
					:view="model.activeView"
					@view="model.setDetailView"
					@page="model.changeDetailPage"
				/>
				<CalendarReconciliation :snapshot="model.snapshot" @bucket="model.openBucket" />
			</template>
			<section v-else class="pcal-state pcal-panel" aria-live="polite" :aria-busy="busy">
				<FeatherIcon
					:name="busy ? 'clock' : model.phase === 'forbidden' ? 'lock' : 'calendar'"
					class="size-8"
				/>
				<h2>{{ stateTitle }}</h2>
				<p>{{ stateMessage }}</p>
				<button
					v-if="!busy && model.company && model.phase !== 'forbidden'"
					class="pcal-button pcal-primary"
					@click="model.refresh()"
				>
					Refresh calendar
				</button>
			</section>
		</main>
		<Dialog v-model="accountsOpen" :options="{ title: 'Bank & cash accounts', size: 'md' }"
			><template #body-content
				><div class="pcal-dialog">
					<p>
						Choose up to 50 accounts in {{ model.company }}. Balances use the company
						currency.
					</p>
					<p v-if="!model.caps?.accounts.length">
						No permitted enabled bank or cash accounts are available.
					</p>
					<div class="account-choices">
						<label v-for="item in model.caps?.accounts || []" :key="item.name"
							><input
								v-model="accountDraft"
								type="checkbox"
								:value="item.name"
								:disabled="
									!accountDraft.includes(item.name) && accountDraft.length >= 50
								"
							/><span
								>{{ item.account_name || item.name
								}}<small
									>{{ item.name }} · {{ item.account_currency }}</small
								></span
							></label
						>
					</div>
					<p>{{ accountDraft.length }} of 50 selected</p>
				</div></template
			><template #actions
				><div class="pcal-dialog-actions">
					<button class="pcal-button" @click="accountsOpen = false">Cancel</button
					><button class="pcal-button pcal-primary" @click="applyAccounts">
						Apply accounts
					</button>
				</div></template
			></Dialog
		>
		<Dialog
			v-model="floorOpen"
			:options="{ title: 'Company minimum cash balance', size: 'md' }"
			><template #body-content
				><div class="pcal-dialog">
					<p>
						This minimum is shared by everyone viewing {{ model.company }}. It changes
						the warning threshold, not accounting balances.
					</p>
					<label class="pcal-field"
						>Minimum ({{ model.caps?.settings?.currency }})<input
							v-model="floorDraft"
							type="text"
							inputmode="decimal"
							autocomplete="off"
							:disabled="model.floorSaving"
					/></label>
					<p>Current minimum: {{ configuredFloor }}</p>
					<p v-if="floorValidation" class="pcal-inline-error" role="alert">
						{{ floorValidation }}
					</p>
					<p v-if="model.floorError" class="pcal-inline-error" role="alert">
						{{ explain(model.floorError.code) }}
					</p>
				</div></template
			><template #actions
				><div class="pcal-dialog-actions">
					<button
						class="pcal-button"
						:disabled="model.floorSaving"
						@click="floorOpen = false"
					>
						Cancel</button
					><button
						class="pcal-button pcal-primary"
						:disabled="model.floorSaving"
						@click="saveFloor"
					>
						{{ model.floorSaving ? "Saving…" : "Save minimum" }}
					</button>
				</div></template
			></Dialog
		>
		<Dialog
			:model-value="!!model.bucket && !!model.snapshot"
			:options="{ title: bucketTitle, size: '4xl' }"
			@update:model-value="
				(value) => {
					if (!value) model.closeBucket();
				}
			"
			><template #body-content
				><div class="pcal-dialog pcal-root">
					<CalendarDetails
						v-if="model.bucket && model.snapshot"
						:snapshot="model.snapshot"
						:data="model.bucketData"
						:loading="model.bucketLoading"
						view="terms"
						:title="bucketTitle"
						:bucket-side="model.bucket.side"
						@page="model.changeBucketPage"
					/></div></template
		></Dialog>
	</div>
</template>
<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Breadcrumbs, Dialog, FeatherIcon } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import { usePaymentCalendar } from "@/composables/usePaymentCalendar";
import { useShellStore } from "@/stores/shell";
import { siteTimezone as timezone } from "@/utils/datetime";
import { snapshotTime } from "@/lib/paymentCalendar/dates";
import { exactMoney } from "@/lib/paymentCalendar/format";
import CalendarGrid from "./CalendarGrid.vue";
import CalendarKpis from "./CalendarKpis.vue";
import UpcomingPayments from "./UpcomingPayments.vue";
import CalendarDetails from "./CalendarDetails.vue";
import CalendarReconciliation from "./CalendarReconciliation.vue";
import { explain } from "./messages";
import "./payment-calendar.css";
const route = useRoute(),
	shell = useShellStore();
const model = reactive(
	usePaymentCalendar({
		initialDate: typeof route.query.date === "string" ? route.query.date : "",
	})
);
const mode = ref(shell.mobile ? "list" : "month");
const accountsOpen = ref(false),
	accountDraft = ref([]),
	floorOpen = ref(false),
	floorDraft = ref(""),
	floorValidation = ref("");
const breadcrumbs = [
	{ label: "Dashboards", route: { name: "DashboardsPage", hash: "#saved" } },
	{ label: "Payment Calendar" },
];
const busy = computed(() =>
	["loading", "loading_company", "requesting", "queued", "running"].includes(model.phase)
);
const configuredFloor = computed(() =>
	exactMoney(
		model.caps?.settings?.configured ? model.caps.settings.safety_floor : null,
		model.caps?.settings?.currency
	)
);
const stateTitle = computed(
	() =>
		({
			loading: "Loading your companies…",
			loading_company: "Checking company access…",
			requesting: "Starting your calendar…",
			queued: "Calendar queued",
			running: "Preparing your calendar",
			choose_company: "Choose a company to begin",
			forbidden: "Finance access required",
			expired: "Snapshot expired",
			failed: "Calendar unavailable",
		}[model.phase] || "Calendar unavailable")
);
const stateMessage = computed(() =>
	busy.value
		? "Reading complete ERPNext balances. Large companies can take a few minutes. You can change company while this runs."
		: model.phase === "choose_company"
		? "Select a permitted company, then choose the bank and cash accounts to include."
		: explain(model.error?.code || (model.phase === "forbidden" ? "forbidden" : "expired"))
);
const bucketTitle = computed(
	() =>
		`${model.bucket?.side === "Receivable" ? "Customer" : "Supplier"} ${
			{
				overdue: "overdue balances",
				undated: "balances without a due date",
				adjustments: "signed adjustments",
				outside_horizon: "balances outside the forecast",
			}[model.bucket?.name] || "balances"
		}`
);
function openAccounts() {
	accountDraft.value = [...model.accounts];
	accountsOpen.value = true;
}
function applyAccounts() {
	accountsOpen.value = false;
	void model.setAccounts(accountDraft.value);
}
function openFloor() {
	floorDraft.value = model.caps?.settings?.configured ? model.caps.settings.safety_floor : "";
	floorValidation.value = "";
	model.floorError = null;
	floorOpen.value = true;
}
async function saveFloor() {
	if (!/^\d+(?:\.\d+)?$/.test(floorDraft.value.trim())) {
		floorValidation.value = "Enter a non-negative number without separators.";
		return;
	}
	floorValidation.value = "";
	const chosenCompany = model.company;
	const result = await model.saveFloor(floorDraft.value.trim());
	if (result?.ok && model.company === chosenCompany) floorOpen.value = false;
}
watch(
	() => model.company,
	() => {
		accountsOpen.value = false;
		floorOpen.value = false;
		floorValidation.value = "";
	}
);
watch(
	() => model.caps,
	(value) => {
		if (!value) {
			accountsOpen.value = false;
			floorOpen.value = false;
			floorDraft.value = "";
		}
	}
);
onMounted(() => {
	void model.initialize();
});
</script>
