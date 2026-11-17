<template>
	<div class="pcal-root flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
		<LayoutHeader
			><template #left-header><Breadcrumbs :items="breadcrumbs" /></template
		></LayoutHeader>
		<main class="pcal-main" aria-labelledby="pcal-title">
			<!-- The page padding is on .pcal-main; .pcal-layout is unpadded, so its container
			     width equals the template's content width. -->
			<div class="pcal-layout">
				<!-- Above the heading row, so the Refresh button beside the title does not move. A plain
				     button: the page's own focus ring (.pcal-root button:focus-visible) is its only ring. -->
				<div v-if="chatConv" class="pcal-back-row">
					<button type="button" class="pcal-text-button pcal-back" @click="backToChat">
						<FeatherIcon name="arrow-left" class="size-4" aria-hidden="true" />{{
							backLabel
						}}
					</button>
				</div>
				<div class="pcal-page-heading">
					<h1 id="pcal-title" tabindex="-1">Payment Calendar</h1>
					<div class="pcal-heading-tools">
						<button
							id="theme-toggle"
							type="button"
							class="pcal-btn pcal-theme-toggle"
							:aria-label="themeLabel"
							:title="themeLabel"
							:aria-pressed="String(dark)"
							@click="toggleTheme()"
						>
							<svg
								viewBox="0 0 24 24"
								:data-icon="dark ? 'sun' : 'moon'"
								aria-hidden="true"
							>
								<template v-if="dark">
									<circle cx="12" cy="12" r="4" />
									<path
										d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"
									/>
								</template>
								<path
									v-else
									d="M20.6 13A8.5 8.5 0 0 1 11 3.4 8.5 8.5 0 1 0 20.6 13Z"
								/>
							</svg>
						</button>
						<span class="pcal-updated"
							><i></i
							><span v-if="model.snapshot" class="pcal-as-of"
								>As of
								{{ snapshotStamp(model.snapshot.generated_at, timezone()) }}</span
							><span v-if="updateNote" role="status">{{ updateNote }}</span
							><span v-if="!model.snapshot">{{
								model.refreshIssue
									? "Waiting for fresh balances…"
									: "Loading current balances…"
							}}</span></span
						>
						<button
							id="refresh-button"
							type="button"
							class="pcal-btn"
							:disabled="!model.company || busy || model.refreshing"
							@click="model.refresh({ background: true })"
						>
							<svg viewBox="0 0 24 24" aria-hidden="true">
								<path d="M20 7v5h-5M4 17v-5h5" />
								<path d="M6.1 6.5A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.5" /></svg
							><span>{{ model.refreshing ? "Refreshing…" : "Refresh" }}</span>
						</button>
					</div>
				</div>
				<CalendarHeadlines
					v-if="viewSnapshot"
					:snapshot="viewSnapshot"
					:month="model.month"
					@jump="jumpToTrough"
					@view-totals="viewTotals"
					@obligation="showObligation"
				/>
				<CalendarFilterBar
					:companies="model.companies"
					:company="model.company"
					:caps="model.caps"
					:accounts="model.accounts"
					:company-disabled="model.phase === 'loading'"
					:floor-saving="model.floorSaving"
					:floor-error="model.floorError"
					:save-floor="saveFloor"
					@choose-company="chooseCompany"
					@set-accounts="model.setAccounts"
				/>
				<p v-if="model.preferenceError" class="pcal-inline-error" role="status">
					Your selection could not be saved.
					<button class="pcal-text-button" @click="model.saveSelection()">
						Retry saving
					</button>
				</p>
				<p v-if="model.notice" class="pcal-notice" role="status">{{ model.notice }}</p>
				<template v-if="viewSnapshot">
					<BookCashRow
						:snapshot="viewSnapshot"
						:timezone="timezone()"
						@open="balancesOpen = true"
					/>
					<CashDetails
						v-model:scope="cashScope"
						v-model:open="detailsOpen"
						:snapshot="viewSnapshot"
						:selected="model.selectedDate"
						:month="model.month"
						:timezone="timezone()"
						@balances="balancesOpen = true"
						@balance-details="method?.openBalanceDetails()"
						@obligation="showObligation"
						@invoices="showInvoices"
						@jump="jumpToRisk"
					/>
					<div
						v-if="model.snapshot && !model.snapshot.projection.forecast_available"
						class="pcal-warning"
						role="status"
					>
						<strong>Cash forecast unavailable</strong>
						<p v-for="reason in model.snapshot.projection.scope_reasons" :key="reason">
							{{ explain(reason) }}
						</p>
						<p v-if="model.snapshot.bank_code">
							{{ explain(model.snapshot.bank_code) }}
						</p>
					</div>
					<p v-if="!model.accounts.length" class="pcal-notice">
						Choose bank and cash accounts to include book cash in the forecast.
					</p>
					<div class="pcal-calendar-layout">
						<div class="pcal-calendar-column">
							<CalendarGrid
								ref="calendarGrid"
								v-model:mode="gridMode"
								:snapshot="viewSnapshot"
								:selected="model.selectedDate"
								:month="model.month"
								:loading="model.refreshing"
								:waiting="model.waitingForWorker"
								:load-error="!!model.refreshIssue"
								:issue="model.refreshIssue"
								@retry="model.refresh({ background: true })"
								@select="selectDate"
								@browse="model.browseMonth"
								@today="goToday"
							/>
							<NextPayments
								:snapshot="viewSnapshot"
								:selected="model.selectedDate"
								:month="model.month"
								:rows="model.upcoming"
								:load-error="!!model.refreshIssue"
								:loading="
									model.upcomingLoading || (!model.snapshot && model.refreshing)
								"
								@select="(date) => selectDate(date, 'Payable')"
								@view-all="viewAllPayments"
							/>
						</div>
						<aside class="pcal-aside">
							<DayLedger
								ref="dayLedger"
								:snapshot="viewSnapshot"
								:scope="ledger.panelScope"
								:side="ledger.panelSide"
								:date="model.selectedDate"
								:panel="ledger.panel"
								@side="ledger.setSide"
								@back="ledger.backToDate"
								@view-all="ledger.openModal()"
								@retry="ledger.retry"
							/>
							<CalendarMethod
								ref="method"
								:snapshot="viewSnapshot"
								:selected="model.selectedDate"
								:scope="cashScope"
								:month="model.month"
								:timezone="timezone()"
							/>
						</aside>
					</div>
					<p class="bottom-note">
						<span>Amounts are remaining balances as of the snapshot.</span>
						<button
							id="snapshot-note"
							type="button"
							@click="method?.openCalculation()"
						>
							View calculation example
						</button>
					</p>
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
						class="pcal-primary-btn"
						@click="model.refresh()"
					>
						Refresh calendar
					</button>
				</section>
			</div>
		</main>
		<CalendarToast :message="toastMessage" @hidden="toastMessage = ''" />
		<AccountBalancesDialog
			v-if="viewSnapshot"
			v-model="balancesOpen"
			:snapshot="viewSnapshot"
			:company="model.company"
			:timezone="timezone()"
		/>
		<InvoiceDetailsDialog
			v-if="viewSnapshot"
			:snapshot="viewSnapshot"
			:modal="ledger.modal"
			:sort-key="ledger.sortKey"
			:sort-direction="ledger.sortDirection"
			@close="ledger.closeModal"
			@side="ledger.setModalSide"
			@sort="ledger.setSort"
			@page="ledger.changeModalPage"
			@retry="ledger.retry"
		/>
	</div>
</template>
<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Breadcrumbs, FeatherIcon } from "frappe-ui";
import { __ } from "@/lib/i18n";
import { useJarvisTheme } from "@/theme";
import LayoutHeader from "@/components/LayoutHeader.vue";
import { usePaymentCalendar } from "@/composables/usePaymentCalendar";
import { usePaymentLedger } from "@/composables/usePaymentLedger";
import { siteTimezone as timezone } from "@/utils/datetime";
import { isSelectable, snapshotStamp } from "@/lib/paymentCalendar/dates";
import CalendarGrid from "./CalendarGrid.vue";
import CalendarHeadlines from "./CalendarHeadlines.vue";
import CalendarFilterBar from "./CalendarFilterBar.vue";
import BookCashRow from "./BookCashRow.vue";
import AccountBalancesDialog from "./AccountBalancesDialog.vue";
import NextPayments from "./NextPayments.vue";
import CashDetails from "./CashDetails.vue";
import CalendarMethod from "./CalendarMethod.vue";
import CalendarToast from "./CalendarToast.vue";
import DayLedger from "./DayLedger.vue";
import InvoiceDetailsDialog from "./InvoiceDetailsDialog.vue";
import { explain } from "./messages";
import { provideOverlayStack } from "./overlayStack";
import "./tokens.css";
import "./PaymentCalendarPage.css";
const route = useRoute(),
	router = useRouter();
// The conversation the hand-off came from (history state { chatConv }); empty on a direct visit.
// Read in setup, so the Back button is on the first paint; a string only (state is user-reachable).
const handedConv = window.history.state?.chatConv;
const chatConv = ref(typeof handedConv === "string" ? handedConv : "");
const backLabel = __("Back to chat");
const CONVERSATION_ROUTE = "Conversation";
// Back in history when the previous entry is that conversation, so the chat keeps its scroll
// position; a push otherwise (a deep link, or the user wandered since).
function backToChat() {
	const target = { name: CONVERSATION_ROUTE, params: { id: chatConv.value } };
	if (window.history.state?.back === router.resolve(target).fullPath) router.back();
	else router.push(target);
}
// The ledger needs the raw core (refs, not unwrapped); only the template reads reactive views.
const core = usePaymentCalendar({
	initialDate: typeof route.query.date === "string" ? route.query.date : "",
});
const model = reactive(core);
const ledger = reactive(usePaymentLedger(core));
// One Escape listener for every overlay of the page (components register through useOverlay).
provideOverlayStack();
const { effectiveDark: dark, toggleTheme } = useJarvisTheme();
// A fixed name: aria-pressed says whether it is on, so the label must not flip as well.
const themeLabel = "Dark mode";
const viewSnapshot = computed(() => model.snapshot || model.loadingSnapshot);
const balancesOpen = ref(false);
const dayLedger = ref(null);
// Cash details: "day" or "month" scope, shared with the calculation example.
const cashScope = ref("day");
// Kept here, not in the details: a floor save unmounts the panel while the snapshot reloads.
const detailsOpen = ref(false);
const method = ref(null);
// The calendar view the page asks for (Next payments → List); null leaves the grid's default.
const gridMode = ref(null);
const toastMessage = ref("");
// Keep in sync with SIDES in usePaymentLedger.js.
const SIDES = ["Payable", "Receivable"];
// An amount line carries its side; set it before the date change so the click reads once.
// A date the core refuses (outside the range) changes nothing, not even the side.
function selectDate(date, side) {
	if (SIDES.includes(side) && isSelectable(date, viewSnapshot.value?.scope))
		ledger.panelSide = side;
	return model.selectDate(date);
}
// "View {date}" from the Lowest Cushion card: select the date on the Payable side, then land on it.
const calendarGrid = ref(null);
function jumpToTrough(date) {
	cashScope.value = "day";
	selectDate(date, "Payable");
	return calendarGrid.value?.revealTrough(date);
}
// Today shows the snapshot day in the day view, like the template.
function goToday() {
	cashScope.value = "day";
	return model.goToday();
}
const viewTotals = () => ledger.openModal({ scope: "month" });
// Show a scope in the side panel, bring the panel into view and focus its heading. The
// obligation cards scroll it to the top; "nearest" (the date's invoices) moves it the least.
async function showObligation(scope, block = "start") {
	const reading = ledger.setScope(scope);
	await nextTick();
	const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
	const panel = dayLedger.value?.$el;
	panel?.scrollIntoView?.({ block, behavior: reduced ? "auto" : "smooth" });
	panel?.querySelector("#ledger-heading")?.focus({ preventScroll: true });
	await reading;
}
// "View invoices" and the two amounts of the cash details: the month scope opens the full
// dialog, the day scope shows the date in the side panel.
function showInvoices(side) {
	if (cashScope.value === "month") return ledger.openModal({ scope: "month", side });
	if (SIDES.includes(side)) ledger.panelSide = side;
	return showObligation("date", "nearest");
}
// The risk alert's "View {date}": select it in day scope and focus its cell.
async function jumpToRisk(date) {
	await selectDate(date);
	await nextTick();
	calendarGrid.value?.$el?.querySelector(`[data-date="${date}"]`)?.focus();
}
// Reset first: the grid may already hold "list" in its own state after a trough jump.
async function viewAllPayments() {
	gridMode.value = null;
	await nextTick();
	gridMode.value = "list";
}
async function saveFloor(value) {
	const result = await model.saveFloor(value);
	if (result?.ok) toastMessage.value = "Minimum cash balance saved.";
	return result;
}
// Confirm at once: loading the company's snapshot can take minutes.
function chooseCompany(name) {
	const changed = !!name && name !== model.company;
	const pending = model.chooseCompany(name);
	if (changed) toastMessage.value = "Company changed.";
	return pending;
}
const breadcrumbs = [
	{ label: "Dashboards", route: { name: "DashboardsPage", hash: "#saved" } },
	{ label: "Payment Calendar" },
];
// The status next to "As of": only while a snapshot is shown and its update is running or stuck.
const updateNote = computed(() => {
	if (!model.snapshot) return "";
	return model.refreshIssue ? "Update paused" : model.refreshing ? "Refreshing data…" : "";
});
const busy = computed(() =>
	["loading", "loading_company", "requesting", "queued", "running"].includes(model.phase)
);
// After the automatic recovery cap both "expired" and "failed" ask for a manual Refresh.
const stateTitle = computed(
	() =>
		(model.error?.code === "recovery_limit" && "Calendar keeps changing") ||
		{
			loading: "Loading your companies…",
			loading_company: "Checking company access…",
			requesting: "Starting your calendar…",
			queued: "Calendar queued",
			running: "Preparing your calendar",
			choose_company: "Choose a company to begin",
			forbidden: "Finance access required",
			expired: "Calendar data expired",
			failed: "Calendar unavailable",
		}[model.phase] ||
		"Calendar unavailable"
);
const stateMessage = computed(() =>
	model.waitingForWorker
		? "Waiting for calendar data. This page will update automatically."
		: busy.value
		? "Reading your balances. Large companies can take a few minutes. You can change company while this runs."
		: model.phase === "choose_company"
		? "Select a permitted company, then choose the bank and cash accounts to include."
		: explain(model.error?.code || (model.phase === "forbidden" ? "forbidden" : "expired"))
);
// The balance rows belong to one company: close the dialog when it changes or access is lost.
watch([() => model.company, () => !!model.caps], () => (balancesOpen.value = false));
onMounted(() => {
	void model.initialize();
	// Arriving from the chat hand-off (ChatView passes history state { fromChat: true, chatConv }):
	// move focus to the title so a keyboard / screen-reader user lands on the page,
	// not on the chat they just left. The flag is one-shot: clear it so a reload or
	// a Forward onto this entry does not pull focus again.
	if (window.history.state?.fromChat === true) {
		document.getElementById("pcal-title")?.focus();
		window.history.replaceState({ ...window.history.state, fromChat: undefined }, "");
	}
});
</script>
