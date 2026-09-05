<template>
	<!-- paletteVars + jv-dark are bound on the page root, not because this page
	     uses jv- markup (it is built from frappe-ui tokens like every other page)
	     but because JvSpinner's label styling reads --text-2. Those vars are NOT
	     on :root and its literal fallback is a light-mode grey, so an unbound
	     overlay label renders wrong in dark mode. Binding here covers the
	     overlay and anything added later. -->
	<div
		class="flex h-full flex-col overflow-hidden"
		:class="{ 'jv-dark': dark }"
		:style="paletteVars"
	>
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: 'Billing' }]" />
			</template>
		</LayoutHeader>

		<div class="min-h-0 flex-1 overflow-y-auto">
			<div class="mx-auto w-full max-w-6xl px-6 py-8">
				<div v-if="loading" class="flex flex-col items-center gap-3 py-24">
					<JvSpinner :size="36" label="Loading your plan…" />
				</div>

				<div v-else-if="loadErr" class="flex flex-col items-center gap-3 py-24">
					<ErrorMessage :message="loadErr" />
					<Button
						variant="subtle"
						label="Retry"
						iconLeft="refresh-cw"
						@click="loadAccount"
					/>
				</div>

				<template v-else>
					<header class="mb-7">
						<h1 class="text-2xl font-semibold text-ink-gray-9">Billing</h1>
						<p class="mt-1 text-p-base text-ink-gray-6">
							Compare plans and change yours. Upgrades take effect straight away;
							switching to a smaller plan applies at your next billing cycle.
						</p>
					</header>

					<!-- Current state, stated once, above the cards. The cards answer
					     "what could I be on"; this answers "what am I on now". -->
					<section
						v-if="currentPlan"
						class="mb-6 rounded-lg border border-outline-gray-1 p-4.5"
					>
						<div class="flex flex-wrap items-center gap-3">
							<span class="text-base font-medium text-ink-gray-8">
								{{ currentPlan.plan_name }}
							</span>
							<span class="text-base text-ink-gray-6">
								{{
									planPriceLabel(
										currentPlan.price_inr,
										currentPlan.billing_cycle
									)
								}}
							</span>
							<!-- GST-exclusive pricing: the price above is the base rate, not
							     what gets charged, so this says so plainly - but only for a
							     plan that actually carries GST. A 0-GST plan, or any plan
							     before get_plans sends gst_percent at all, must not claim an
							     exemption it doesn't have. -->
							<span v-if="planHasGst(currentPlan)" class="text-p-sm text-ink-gray-5"
								>excl. GST</span
							>
							<Badge
								variant="subtle"
								:theme="statusTheme"
								:label="
									cancelling
										? cancelPillLabel(account.access_ends_on)
										: statusLabel(account.subscription_status)
								"
							/>
						</div>
						<p class="mt-1 text-p-sm text-ink-gray-6">
							{{ renewalLabel(account.current_period_end, account.days_remaining)
							}}<template v-if="account.autorenew && !cancelling">
								· Auto-renew on</template
							>
						</p>
					</section>

					<!-- Blocking states first: while a cancellation is pending the
					     server refuses upgrades outright (ResumeBeforeUpgrade), so
					     Resume has to be reachable from the page that offers them. -->
					<BillingNotice
						v-if="cancelling"
						:message="cancellationNotice(account.access_ends_on)"
						action-label="Resume"
						solid
						:loading="busy === 'resume'"
						@action="doResume"
					/>

					<BillingNotice
						v-if="scheduledDowngrade"
						:message="scheduledDowngradeNotice"
						action-label="Keep current plan"
						:loading="busy === 'keep'"
						@action="doKeepCurrentPlan"
					/>

					<BillingNotice
						v-if="account.can_reauthorize"
						:message="reauthBanner"
						action-label="Set up auto-renewal"
						:loading="busy === 'reauth'"
						@action="doReauthorize"
					/>

					<!-- F12: next to the controls above that raise it, not the page foot. -->
					<ErrorMessage v-if="actionErr" class="mb-4" :message="actionErr" />

					<!-- The unknown-code branch's own notice: whatever code the last
					     billing answer carried, rendered honestly with its declared
					     actions instead of the silent reload this replaced. -->
					<div
						v-if="codeNotice"
						class="mb-4 rounded-md border border-outline-gray-1 p-4"
					>
						<p class="text-p-base font-medium text-ink-gray-8">
							{{ codeNotice.copy.headline }}
						</p>
						<p class="mt-1 text-p-sm text-ink-gray-6">{{ codeNotice.copy.body }}</p>
						<div
							v-if="codeNotice.copy.actions.length"
							class="mt-3 flex flex-wrap gap-2"
						>
							<Button
								v-for="(action, i) in codeNotice.copy.actions"
								:key="action"
								:variant="i === 0 ? 'solid' : 'subtle'"
								:label="actionLabelFor(codeNotice.copy, action)"
								:loading="action === ACTIONS.CHECK && busy === 'check'"
								:disabled="!!busy"
								@click="runCodeAction(action)"
							/>
						</div>
					</div>

					<p
						v-if="notice"
						class="mb-4 rounded-md border border-outline-gray-1 p-4 text-p-sm text-ink-gray-7"
					>
						{{ notice }}
					</p>

					<h2 class="mb-3 mt-8 text-lg font-semibold text-ink-gray-9">Plans</h2>
					<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
						<PlanCard
							v-if="currentPlan"
							:plan="currentPlan"
							current
							badge="Current plan"
							:action-label="currentAction.label"
							:note="currentAction.note"
							:loading="busy === 'renew'"
							@action="doRenew"
						/>
						<!-- can_reactivate: the lapsed cohort buys a plan outright (no
						     proration, nothing scheduled), so it gets its own grid instead
						     of the prorated upgrade/downgrade cards below. -->
						<template v-if="canReactivate">
							<PlanCard
								v-for="p in reactivationPlans"
								:key="'react-' + p.name"
								:plan="p"
								action-label="Renew on this plan"
								:note="REACTIVATION_NOTE"
								:loading="busy === 'react:' + p.name"
								@action="doReactivate"
							/>
						</template>
						<template v-else>
							<PlanCard
								v-for="p in upgradePlans"
								:key="'up-' + p.name"
								:plan="p"
								action-label="Upgrade"
								note="You pay only the prorated difference for the days left in this period."
								:disabled="changesBlocked"
								:loading="busy === 'up:' + p.name"
								@action="doUpgrade"
							/>
							<PlanCard
								v-for="p in downgradePlans"
								:key="'down-' + p.name"
								:plan="p"
								action-label="Switch to this plan"
								note="Applies at your next billing cycle. You keep your current plan until then."
								:disabled="changesBlocked"
								:loading="busy === 'down:' + p.name"
								@action="doDowngrade"
							/>
						</template>
					</div>

					<p v-if="changesBlockedReason" class="mt-3 text-p-sm text-ink-gray-6">
						{{ changesBlockedReason }}
					</p>
					<p v-if="noOtherPlans" class="mt-4 text-p-sm text-ink-gray-6">
						There are no other plans available on your account right now.
					</p>

					<!-- Billing details (Phase 3b): the customer's own editable GST party. -->
					<section
						v-if="profile || profileErr"
						class="mt-8 rounded-lg border border-outline-gray-1 p-5"
					>
						<h2 class="text-lg font-semibold text-ink-gray-9">Invoicing details</h2>
						<div v-if="profileErr">
							<ErrorMessage :message="profileErr" />
							<Button
								class="mt-3"
								variant="subtle"
								label="Try again"
								@click="loadBillingProfile"
							/>
						</div>
						<template v-else>
							<p class="mt-1 text-p-sm text-ink-gray-6">
								The company your GST invoice is raised to. Leave the name/email
								blank to use your own company and account email. Changes apply to
								future invoices.
							</p>
							<div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
								<FormControl
									class="sm:col-span-2"
									label="Invoicing company name"
									placeholder="Defaults to your company name"
									:model-value="billingForm.company_name"
									@update:model-value="(v) => (billingForm.company_name = v)"
								/>
								<FormControl
									class="sm:col-span-2"
									type="email"
									label="Invoicing email"
									:placeholder="profile.account_email || 'billing@company.com'"
									:model-value="billingForm.email"
									@update:model-value="(v) => (billingForm.email = v)"
								/>
								<FormControl
									label="Contact person"
									:model-value="billingForm.contact_person"
									@update:model-value="(v) => (billingForm.contact_person = v)"
								/>
								<FormControl
									label="Contact number"
									:model-value="billingForm.contact_number"
									@update:model-value="(v) => (billingForm.contact_number = v)"
								/>
								<FormControl
									class="sm:col-span-2"
									label="Billing address"
									placeholder="Street, area"
									:model-value="billingForm.address_line1"
									@update:model-value="(v) => (billingForm.address_line1 = v)"
								/>
								<FormControl
									class="sm:col-span-2"
									label="Address line 2"
									placeholder="Landmark, area (optional)"
									:model-value="billingForm.address_line2"
									@update:model-value="(v) => (billingForm.address_line2 = v)"
								/>
								<FormControl
									label="City"
									placeholder="Chennai"
									:model-value="billingForm.city"
									@update:model-value="(v) => (billingForm.city = v)"
								/>
								<FormControl
									label="Pincode"
									placeholder="600001"
									:model-value="billingForm.pincode"
									@update:model-value="(v) => (billingForm.pincode = v)"
								/>
								<FormControl
									v-if="billingIsIndia"
									type="select"
									label="State"
									:options="billingStateOptions"
									:model-value="billingForm.state"
									@update:model-value="(v) => (billingForm.state = v)"
								/>
								<FormControl
									v-else
									type="text"
									label="State / Region"
									:model-value="billingForm.state"
									@update:model-value="(v) => (billingForm.state = v)"
								/>
								<FormControl
									type="select"
									label="Country"
									:options="billingCountryOptions"
									:model-value="billingForm.country || 'India'"
									@update:model-value="(v) => (billingForm.country = v)"
								/>
								<FormControl
									label="GSTIN"
									placeholder="33ABCDE1234F1Z7"
									:model-value="billingForm.gstin"
									@update:model-value="
										(v) => (billingForm.gstin = (v || '').toUpperCase())
									"
								/>
							</div>
							<div class="mt-4 flex items-center gap-3">
								<Button
									variant="solid"
									label="Save billing details"
									:loading="billingSaving"
									@click="saveBillingDetails"
								/>
								<span v-if="billingSaved" class="text-p-sm text-ink-green-3"
									>Saved.</span
								>
							</div>
							<ErrorMessage class="mt-2" :message="billingErr" />
						</template>
					</section>

					<!-- Invoices (Phase 3b) -->
					<section class="mt-6 rounded-lg border border-outline-gray-1 p-5">
						<h2 class="text-lg font-semibold text-ink-gray-9">Invoices</h2>
						<div v-if="invoicesLoading" class="flex justify-center py-6">
							<JvSpinner :size="24" label="Loading invoices…" />
						</div>
						<template v-else>
							<ErrorMessage v-if="invoicesErr" :message="invoicesErr" />
							<p
								v-else-if="!invoices.length"
								class="mt-2 text-p-base text-ink-gray-6"
							>
								No invoices yet. Your GST invoice will appear here after your next
								payment.
							</p>
							<div v-else class="mt-3 divide-y divide-outline-gray-1">
								<div
									v-for="inv in invoices"
									:key="inv.erp_name"
									class="flex items-center justify-between py-3"
								>
									<div>
										<div class="text-base font-medium text-ink-gray-8">
											{{ inv.invoice_no }}
										</div>
										<div class="text-p-sm text-ink-gray-6">
											<span v-if="inv.date">{{ inv.date }}</span
											><span v-if="inv.date && inv.total_inr != null">
												&middot; </span
											><span v-if="inv.total_inr != null">{{
												inrExact(inv.total_inr)
											}}</span>
										</div>
									</div>
									<Button
										variant="subtle"
										iconLeft="download"
										:label="
											downloadingErp === inv.erp_name
												? 'Preparing…'
												: 'Download'
										"
										:loading="downloadingErp === inv.erp_name"
										@click="onDownloadInvoice(inv.erp_name)"
									/>
								</div>
							</div>
						</template>
					</section>
				</template>
			</div>
		</div>

		<!-- Confirm step. The customer sees the exact amount HERE, before they
		     leave for the admin-hosted pay page.

		     Explicit binding rather than v-model: EVERY close path (Cancel,
		     Escape, backdrop) has to run closePending so an in-flight preview
		     token is invalidated, not just the Cancel button. -->

		<Dialog
			:modelValue="pending.open"
			:options="{ title: pending.title, size: 'sm' }"
			@update:modelValue="(v) => (v ? null : closePending())"
		>
			<template #body-content>
				<div v-if="pending.loading" class="flex justify-center py-6">
					<JvSpinner :size="28" label="Working out your price…" />
				</div>
				<template v-else>
					<p v-if="pending.amount" class="text-2xl font-semibold text-ink-gray-9">
						{{ pending.amount }}
					</p>
					<p class="mt-1 text-p-base text-ink-gray-6">{{ pending.message }}</p>
				</template>
			</template>
			<template #actions>
				<div class="flex justify-end gap-2">
					<Button label="Cancel" @click="closePending" />
					<Button
						variant="solid"
						:label="pending.confirmLabel"
						:disabled="pending.loading"
						@click="confirmPending"
					/>
				</div>
			</template>
		</Dialog>

		<!-- The copy MOMENT (WS8). Covers the whole surface for the instant
		     between a payable answer and the top-level navigation to the
		     admin-hosted pay page, so nothing behind it can be clicked while the
		     browser is leaving. On a bfcache return it is cleared in onPageShow,
		     so a restored page can never be left stuck here. -->
		<div
			v-if="phase"
			class="fixed inset-0 z-50 grid place-items-center bg-surface-white/85 backdrop-blur-sm"
		>
			<JvSpinner :size="56" :label="phaseLabel" />
		</div>
	</div>
</template>

<script setup>
/**
 * The in-SPA replacement for the /app/jarvis-account desk billing page.
 *
 * The backend was never the blocker: preview_*, start_*, cancel_*, resume and
 * reauthorize were all already whitelisted for the desk page.
 *
 * plan-09 WS8 (the admin-hosted checkout cutover): this page opens NO gateway
 * SDK on its own origin. A payable action (renew / upgrade / re-arm autopay / a
 * Monthly downgrade or its revocation) answers with a pay-page TOKEN plus the
 * bench's OWN attested origin (account.py augment_pay_page), and the customer is
 * TOP-LEVEL NAVIGATED to `{origin}/jarvis-checkout#t=<token>` - the exact
 * mechanism onboarding uses (usePaymentFlow.navigateToPay). There is NO FALLBACK:
 * a token with no attested origin, or a pre-cutover admin's raw handles with no
 * token, fails the action CLOSED with honest copy - never an SDK on this origin.
 * The admin-hosted page + the gateway webhook are authoritative; on return this
 * page just re-reads the account.
 */
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { Badge, Breadcrumbs, Button, Dialog, ErrorMessage, FormControl } from "frappe-ui";
import * as api from "@/api";
import LayoutHeader from "@/components/LayoutHeader.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import PlanCard from "./PlanCard.vue";
import BillingNotice from "./BillingNotice.vue";
import { useJarvisTheme } from "@/theme";
import { errMessage as errMsg } from "@/lib/errors";
// plan-09 WS8: the admin-hosted checkout cutover reuses onboarding's WS7 helpers
// verbatim - the same code-reading (effectiveCode), the same copy table (CODES /
// copyFor), and the same URL builder (payPageUrl). Nothing gateway-specific is
// forked here.
import {
	CLIENT_OFFLINE,
	CLIENT_UNREADABLE,
	decode as decodePaymentResponse,
	effectiveCode,
} from "@/onboarding/paymentCodec";
import {
	ACTIONS,
	CODES,
	TONE,
	UNKNOWN_COPY,
	actionLabelFor,
	copyFor,
} from "@/onboarding/paymentCodes";
import { payPageUrl, STATES as PAY_STATES } from "@/onboarding/paymentMachine";
import {
	inrExact,
	statusLabel,
	statusBadgeTheme,
	planPriceLabel,
	planHasGst,
	renewalLabel,
	cancelPillLabel,
	cancellationNotice,
} from "@/account/format.js";

const { effectiveDark: dark, paletteVars } = useJarvisTheme();

const account = ref({});
const loading = ref(true);
const loadErr = ref("");
const actionErr = ref("");
const notice = ref("");
// The unknown-code branch's own state: the row's copy for whatever code the
// last billing answer carried, so a code with no dedicated branch below still
// renders a headline, body and its declared actions instead of nothing.
const codeNotice = ref(null); // {code, copy} | null
// Which button is spinning, keyed so only the pressed card shows a loader.
const busy = ref("");
// "" | "redirect" - drives the blocking overlay. The only wait this page owns
// now is the copy MOMENT before the browser leaves for the admin-hosted pay page
// (WS8); the payment itself happens there, not here.
const phase = ref("");

const PHASE_LABELS = {
	// Reused verbatim from the payment-code table so onboarding and billing show
	// the same sentence in the instant before the top-level navigation fires.
	redirect: copyFor(CODES.PAYMENT_PAGE_REDIRECT).headline,
};
const phaseLabel = computed(() => PHASE_LABELS[phase.value] || "Working…");

// admin_client._do_post already unwraps the {ok, data} envelope, so a billing
// response is normally a flat dict and this is a no-op; kept as the same
// defensive peel the six call sites used before WS8, so a re-wrapped response
// from the admin plane cannot silently produce an empty answer.
function unwrapData(res) {
	if (res && typeof res === "object" && res.data && typeof res.data === "object") {
		return res.data;
	}
	return res || {};
}

const currentPlan = computed(() => {
	const p = account.value.plan;
	return p && p.plan_name ? p : null;
});
const upgradePlans = computed(() => account.value.upgrade_plans || []);
const downgradePlans = computed(() => account.value.downgrade_plans || []);
const cancelling = computed(() => !!account.value.cancel_at_period_end);
const scheduledDowngrade = computed(() => !!account.value.scheduled_plan);
// The lapsed cohort (Expired/Cancelled/Past-Due-with-no-days-left): a plan
// change here is a fresh full-price purchase, not a prorated switch, so it
// gets its own grid instead of upgrade_plans/downgrade_plans.
const canReactivate = computed(() => !!account.value.can_reactivate);
// reactivation_plans includes the current plan (server contract); it already
// has its own card via currentPlan/doRenew above, so drop it here.
const reactivationPlans = computed(() => {
	const cur = currentPlan.value && currentPlan.value.name;
	return (account.value.reactivation_plans || []).filter((p) => p.name !== cur);
});
const REACTIVATION_NOTE =
	"Full price for this plan, whether it costs more or less than today. Nothing is prorated, and nothing is scheduled.";
const noOtherPlans = computed(() =>
	canReactivate.value
		? reactivationPlans.value.length === 0
		: !upgradePlans.value.length && !downgradePlans.value.length
);

// Plan changes are refused server-side while a cancellation or a switch is
// already pending, so the cards disable rather than offer a button that 400s.
const changesBlocked = computed(() => cancelling.value || scheduledDowngrade.value);

// Shared with PlanBillingPane via format.js rather than copied. The status
// colour rule living in two places is how the settings pane and this page end
// up showing different badges for the same subscription.
const statusTheme = computed(() =>
	statusBadgeTheme(account.value.subscription_status, cancelling.value)
);

// Why the plan actions are inert. The retired pane HID them in these states,
// reasoning that a button which 400s is worse than no button; rendering them
// disabled with no explanation is worse than either, because the customer is
// left to guess whether it is their account or the page that is broken.
const changesBlockedReason = computed(() => {
	if (cancelling.value) {
		return "Your subscription is set to end, so plan changes are paused. Resume it above to change plans.";
	}
	if (scheduledDowngrade.value) {
		return "A plan switch is already scheduled, so further changes are paused. Keep your current plan above to change that.";
	}
	return "";
});

const currentAction = computed(() => {
	// can_reactivate covers the FULL lapsed cohort (Expired/Cancelled/Past-Due
	// with no days left) - Renew-onto-current must work for all three, not just
	// the two statuses the badge happens to render differently for.
	if (canReactivate.value)
		return { label: "Renew", note: "Renewing restores access straight away." };
	if (cancelling.value) return { label: "", note: "Resume above to keep this plan." };
	// Pre-expiry pay-now-to-extend. The server (can_renew / _may_renew) accepts a
	// manual renewal on a still-running subscription that is not autocharging, and
	// activate_and_assign stacks the new period onto the days already left rather
	// than resetting from now. doRenew (the card's @action) sends no target_plan
	// here, which is the same-plan renewal renew() accepts. This is a distinct
	// offer from the "Set up auto-renewal" banner: pay once now vs arm a future
	// mandate. Without it, an Active-with-days-left or Past-Due customer whose
	// autorenew is off had no way to pay before lapsing.
	if (account.value.can_renew)
		return {
			label: "Renew now",
			note: "Adds a full billing period on top of the days you have left.",
		};
	return { label: "", note: "You are on this plan." };
});

const scheduledDowngradeNotice = computed(() => {
	const a = account.value;
	const name = a.scheduled_plan_name || a.scheduled_plan || "a smaller plan";
	const on = (a.scheduled_plan_on || "").split(" ")[0];
	return on
		? `Switching to ${name} on ${on}. You keep your current plan until then.`
		: `Switching to ${name} at your next billing cycle.`;
});

const reauthBanner = computed(() => {
	const endsOn = (account.value.access_ends_on || "").split(" ")[0];
	return endsOn
		? `Auto-renewal is off. Set it up before ${endsOn} to stay subscribed.`
		: "Auto-renewal is off. Set it up before your period ends.";
});

async function loadAccount() {
	loading.value = true;
	loadErr.value = "";
	try {
		account.value = (await api.getAccount()) || {};
	} catch (e) {
		loadErr.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

// ---- Invoices + billing details (Phase 3b) --------------------------------
const invoices = ref([]);
const invoicesLoading = ref(true);
const invoicesErr = ref("");
const downloadingErp = ref("");
const profile = ref(null);
const profileErr = ref("");
const billingForm = reactive({
	company_name: "",
	email: "",
	contact_person: "",
	contact_number: "",
	address_line1: "",
	address_line2: "",
	city: "",
	state: "",
	pincode: "",
	country: "India",
	gstin: "",
});
const billingSaving = ref(false);
const billingSaved = ref(false);
const billingErr = ref("");

// Inlined (not @/onboarding/indianStates, which arrives with the onboarding
// State/Country PR): the GST states for the billing-details Select. Dedupe once
// that file reaches develop.
const BILLING_STATES = [
	"Jammu and Kashmir",
	"Himachal Pradesh",
	"Punjab",
	"Chandigarh",
	"Uttarakhand",
	"Haryana",
	"Delhi",
	"Rajasthan",
	"Uttar Pradesh",
	"Bihar",
	"Sikkim",
	"Arunachal Pradesh",
	"Nagaland",
	"Manipur",
	"Mizoram",
	"Tripura",
	"Meghalaya",
	"Assam",
	"West Bengal",
	"Jharkhand",
	"Odisha",
	"Chhattisgarh",
	"Madhya Pradesh",
	"Gujarat",
	"Dadra and Nagar Haveli and Daman and Diu",
	"Maharashtra",
	"Karnataka",
	"Goa",
	"Lakshadweep",
	"Kerala",
	"Tamil Nadu",
	"Puducherry",
	"Andaman and Nicobar Islands",
	"Telangana",
	"Andhra Pradesh",
	"Ladakh",
	"Other Territory",
];
const BILLING_COUNTRIES = [
	"India",
	"United States",
	"United Kingdom",
	"United Arab Emirates",
	"Singapore",
	"Australia",
	"Canada",
	"Germany",
	"Other",
];
const billingStateOptions = [
	{ label: "Select state…", value: "" },
	...BILLING_STATES.map((s) => ({ label: s, value: s })),
];
const billingCountryOptions = BILLING_COUNTRIES.map((c) => ({ label: c, value: c }));
const billingIsIndia = computed(
	() => (billingForm.country || "India").trim().toLowerCase() === "india"
);

async function loadInvoices() {
	invoicesLoading.value = true;
	invoicesErr.value = "";
	try {
		invoices.value = (await api.getInvoices()) || [];
	} catch (e) {
		invoicesErr.value = errMsg(e);
	} finally {
		invoicesLoading.value = false;
	}
}

async function loadBillingProfile() {
	profileErr.value = "";
	try {
		const p = (await api.getBillingProfile()) || {};
		profile.value = p;
		if (p.bill_to === "customer" && p.billing) {
			for (const k of Object.keys(billingForm)) {
				if (p.billing[k] != null && p.billing[k] !== "") billingForm[k] = p.billing[k];
			}
			if (!billingForm.country) billingForm.country = "India";
		}
	} catch (e) {
		// Surface it (this is the customer's tax-invoice party): the card shows the
		// error + a retry rather than silently vanishing on a transient failure.
		profileErr.value = errMsg(e);
	}
}

// "Saved." (or a prior error) must not linger next to freshly-edited, unsaved fields.
watch(billingForm, () => {
	billingSaved.value = false;
	if (billingErr.value) billingErr.value = "";
});

async function saveBillingDetails() {
	billingSaving.value = true;
	billingErr.value = "";
	billingSaved.value = false;
	try {
		const payload = {};
		for (const [k, v] of Object.entries(billingForm)) {
			payload[k] = (v || "").trim(); // present-but-empty clears; the server normalizer validates
		}
		const out = await api.updateBillingDetails(payload);
		billingSaved.value = true;
		if (out && out.billing) {
			for (const k of Object.keys(billingForm)) {
				billingForm[k] = out.billing[k] || (k === "country" ? "India" : "");
			}
		}
	} catch (e) {
		billingErr.value = errMsg(e);
	} finally {
		billingSaving.value = false;
	}
}

async function onDownloadInvoice(erpName) {
	downloadingErp.value = erpName;
	try {
		const res = await api.downloadInvoice(erpName);
		if (res && res.content_b64) {
			const bytes = Uint8Array.from(atob(res.content_b64), (c) => c.charCodeAt(0));
			const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
			const a = document.createElement("a");
			a.href = url;
			a.download = res.filename || "invoice.pdf";
			document.body.appendChild(a);
			a.click();
			a.remove();
			setTimeout(() => URL.revokeObjectURL(url), 5000);
		}
	} catch (e) {
		invoicesErr.value = errMsg(e);
	} finally {
		downloadingErp.value = "";
	}
}
// Returning from the admin-hosted pay page (WS8). A fresh navigation re-runs
// onMounted and re-reads below; a bfcache back-button restores the DOM WITHOUT
// re-mounting, which would strand the frozen "Taking you to the secure payment
// page…" overlay and show stale plan state. On a persisted restore, clear the
// overlay and re-read server truth (the pay page + webhook are authoritative).
function onPageShow(e) {
	if (e && e.persisted) {
		phase.value = "";
		busy.value = "";
		loadAccount();
	}
}
// The admin pay page appends ?pay=done|failed|pending on its way back here (WS6
// workspace.OUTCOME_*). done/pending mean money moved and the seams may still be
// settling, so a plain read can show the customer the very state they just paid
// to leave - run the healer once. A genuine decline is terminal and has nothing
// to converge on, but the redirect can carry a FALSE `pay=failed` (a server-side
// race between the gateway and the bench), so failed also gets exactly one
// status check below - the same action the manual "Check payment status" button
// runs. A real decline rides the same check straight back to the coded-notice
// presentation (settleWithRedirect), never a spinner or a second check.
function payOutcomeFrom(search) {
	try {
		return new URLSearchParams(search || "").get("pay") || "";
	} catch {
		return "";
	}
}
const CHECK_STATUS_OUTCOMES = new Set(["done", "pending", "failed"]);

onMounted(() => {
	const payOutcome = payOutcomeFrom(window.location.search);
	const runCheck = CHECK_STATUS_OUTCOMES.has(payOutcome);
	// No URL rewrite here. The router already drops the query and hash on mount, so
	// stripping `pay` was dead code that a spec nonetheless pinned - a test asserting
	// behaviour the application never exhibits is worse than no test. The healer runs
	// once from the value read above; a reload cannot re-run it because the parameter
	// is already gone by then.
	loadAccount().then(() => {
		if (runCheck) doCheckStatus();
	});
	window.addEventListener("pageshow", onPageShow);
});
onBeforeUnmount(() => window.removeEventListener("pageshow", onPageShow));
// Invoices + billing details load on mount, independent of the plan read above.
onMounted(() => {
	loadInvoices();
	loadBillingProfile();
});

// ---- the confirm step -------------------------------------------------------
// One dialog serves every flow. `run` is the function to call on confirm, so
// the dialog never needs to know which billing action it is fronting.
const pending = reactive({
	open: false,
	loading: false,
	title: "",
	message: "",
	amount: "",
	confirmLabel: "Confirm",
	run: null,
});

// Bumped every time the dialog is closed, so a preview that resolves AFTER the
// customer walked away cannot pop the dialog back open with a price they are no
// longer asking for.
let pendingToken = 0;

function closePending() {
	pending.open = false;
	pending.loading = false;
	pending.run = null;
	pendingToken += 1;
}

async function confirmPending() {
	const run = pending.run;
	closePending();
	if (run) await run();
}

// ---- flows ------------------------------------------------------------------

async function doUpgrade(plan) {
	await priceThenConfirm({
		key: "up:" + plan.name,
		preview: () => api.previewUpgrade(plan.name),
		title: `Upgrade to ${plan.plan_name || plan.name}`,
		// The prorated figure is the whole point of previewing, so it leads.
		// inrExact, not inr: prorated_inr is a real charge computed server-side
		// (days-left * GST-inclusive daily rate) and can land on paise, same as
		// total_inr below - inr()'s bare toLocaleString would show a stray
		// one-decimal amount instead of the exact figure charged.
		describe: (d) => ({
			amount: inrExact(d.prorated_inr),
			message:
				"Charged now for the days left in your current billing period. Your new plan starts immediately.",
			confirmLabel: `Pay ${inrExact(d.prorated_inr)}`,
		}),
		start: () => api.startUpgrade(plan.name),
		retry: () => doUpgrade(plan),
	});
}

async function doDowngrade(plan) {
	await priceThenConfirm({
		key: "down:" + plan.name,
		preview: () => api.previewDowngrade(plan.name),
		title: `Switch to ${plan.plan_name || plan.name}`,
		// A downgrade takes no money today, so leading with an amount would be a
		// lie. State when it happens instead.
		describe: (d) => ({
			amount: "",
			message: d.effective_on
				? `Your plan changes on ${
						String(d.effective_on).split(" ")[0]
				  }. You keep your current plan until then, and nothing is charged today.`
				: "Your plan changes at your next billing cycle. You keep your current plan until then, and nothing is charged today.",
			confirmLabel: "Schedule switch",
		}),
		start: () => api.startDowngrade(plan.name),
		retry: () => doDowngrade(plan),
	});
}

async function doRenew() {
	// Renew has no preview_* sibling: the amount is simply the plan's price, so
	// the confirm step reads it off the plan rather than minting an order the
	// customer has not agreed to yet. Renew/reactivate charge tax-inclusive
	// server-side, so the confirm dialog must show total_inr (price_inr + GST),
	// not the pre-tax price_inr - otherwise the number shown here understates
	// what actually gets charged. Fall back to price_inr against an older
	// backend that has not started sending total_inr yet.
	const charge = currentPlan.value
		? currentPlan.value.total_inr ?? currentPlan.value.price_inr
		: 0;
	openConfirm({
		title: "Renew subscription",
		// inrExact, not inr: charge is total_inr (or its price_inr fallback),
		// which can carry GST-driven paise precision - the exact figure charged
		// has to render exactly, not with inr()'s bare toLocaleString.
		amount: inrExact(charge),
		message: "Renewing restores access straight away for another full billing period.",
		confirmLabel: `Pay ${inrExact(charge)}`,
		run: () =>
			runPayment({
				key: "renew",
				// The price shown on this card is the current plan's, so name it as the
				// target: renew then prices the order off the same plan instead of a
				// scheduled_plan the customer was never shown. A running subscription
				// sends none - admin refuses a target there (NotLapsed).
				start: () =>
					api.renewPlan(canReactivate.value ? currentPlan.value.name : undefined),
				raw: true,
				retry: doRenew,
			}),
	});
}

/** The lapsed cohort's plan picker (can_reactivate): a plan change here has no
 * period left to prorate or schedule against, so it is one full-price payment
 * that starts a new period - up or down, mechanically identical. */
async function doReactivate(plan) {
	// Same tax-inclusive rule as doRenew above: total_inr is what gets charged,
	// price_inr is only the pre-tax base, so the dialog leads with total_inr and
	// falls back to price_inr against an older backend.
	const charge = plan.total_inr ?? plan.price_inr;
	openConfirm({
		title: `Renew on ${plan.plan_name || plan.name}`,
		// inrExact, not inr: same paise-precision reasoning as doRenew above.
		amount: inrExact(charge),
		message:
			"This charges the plan's full price and starts a new billing period right away. There is no credit for time already lapsed, and nothing is scheduled.",
		confirmLabel: `Pay ${inrExact(charge)}`,
		run: () =>
			runPayment({
				key: "react:" + plan.name,
				start: () => api.renewPlan(plan.name),
				raw: true,
				retry: () => doReactivate(plan),
			}),
	});
}

async function doReauthorize() {
	openConfirm({
		title: "Set up auto-renewal",
		amount: "",
		// Mandate-only checkout. Saying "nothing is charged" is load-bearing: the
		// pay page still shows a payment form and looks like it will take money.
		message:
			"You will confirm a payment method. Nothing is charged now - your current period is already paid for.",
		confirmLabel: "Continue",
		run: () =>
			runPayment({
				key: "reauth",
				start: () => api.reauthorizeAutopay(),
				retry: doReauthorize,
			}),
	});
}

async function doResume() {
	busy.value = "resume";
	actionErr.value = "";
	notice.value = "";
	try {
		const out = unwrapData(await api.resumePlan());
		if (out.requires_reauthorization) {
			// Cancelling released the autopay mandate, and a released mandate is
			// terminal at Razorpay. Resume alone cannot bring auto-renewal back.
			notice.value =
				"Your plan is active again, but auto-renewal is off. Set it up below to stay subscribed.";
		}
		await loadAccount();
	} catch (e) {
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

/** The active healer, then a fresh read of where the checkout stands now
 * (edge 6: the intent can resolve in between). Both ride settleWithRedirect,
 * with autoNavigate off (F2): a status check has not just been confirmed by
 * the customer, so a live token surfaces as Resume rather than an immediate
 * top-level navigate. */
async function doCheckStatus() {
	if (busy.value) return; // edge 1: one Check in flight at a time
	busy.value = "check";
	actionErr.value = "";
	codeNotice.value = null;
	try {
		// The healer's refusals are ANSWERS about the payment (rate-limited, under
		// review), coded under a deliberate 4xx - decode them and render the code
		// itself. Collapsing them into the transport row would tell a throttled
		// customer we could not reach a service we did reach.
		const verdict = decodePaymentResponse(await api.checkBillingPayment());
		if (!verdict.ok) {
			const unreadable =
				verdict.code === CLIENT_OFFLINE || verdict.code === CLIENT_UNREADABLE;
			const code = unreadable ? CODES.BENCH_ADMIN_UNREACHABLE : verdict.code;
			// A coded refusal here whose row offers INITIATE ("Start a new
			// payment") - e.g. BILLING_NO_CURRENT_INTENT - must start a renewal,
			// NOT re-run the status check. Binding retry to doCheckStatus made
			// INITIATE re-check the same absent intent, answering the same 409
			// forever: a button that looked dead. doRenew mirrors this function's
			// own success path (settleWithRedirect below) and runCodeAction's
			// no-notice fallback. Codes whose only action is CHECK are unaffected
			// - runCodeAction routes CHECK straight to doCheckStatus, not retry.
			codeNotice.value = { code, copy: billingCopyFor(code), retry: doRenew };
			await loadAccount();
			return;
		}
		const state = unwrapData(await api.billingPaymentState());
		await settleWithRedirect(state, { autoNavigate: false, retry: doRenew });
	} catch (e) {
		// F3: a transport/admin failure says nothing about the payment - render
		// the bench's own honest code, never _surface's operator prose or a bare
		// "Failed to fetch".
		codeNotice.value = {
			code: CODES.BENCH_ADMIN_UNREACHABLE,
			copy: billingCopyFor(CODES.BENCH_ADMIN_UNREACHABLE),
			retry: doCheckStatus,
		};
	} finally {
		busy.value = "";
	}
}

async function doKeepCurrentPlan() {
	busy.value = "keep";
	actionErr.value = "";
	codeNotice.value = null;
	try {
		const data = unwrapData(await api.cancelScheduledDowngrade());
		// Monthly: revoking the switch also dropped the cheaper mandate, so the
		// current plan's mandate has to be re-armed - the response carries a
		// pay-page token and we navigate to the admin-hosted mandate checkout.
		// Annual returns no token and falls through to a plain reload.
		//
		// The schedule was ALREADY cleared server-side by this call. If the customer
		// navigates to re-arm and abandons it, the honest state is what loadAccount
		// shows on return: the switch is gone and, on Monthly, the reauthorize
		// banner (can_reauthorize) offers auto-renewal again. There is no dismissal
		// to guess at any more - the pay page owns the outcome.
		await settleWithRedirect(data, { retry: doKeepCurrentPlan });
	} catch (e) {
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

// ---- shared machinery -------------------------------------------------------

function openConfirm(opts) {
	pending.open = true;
	pending.loading = false;
	pending.title = opts.title;
	pending.amount = opts.amount || "";
	pending.message = opts.message || "";
	pending.confirmLabel = opts.confirmLabel || "Confirm";
	pending.run = opts.run;
	// F11: a stale error/notice from a previous action must not sit under a
	// freshly opened dialog.
	actionErr.value = "";
	codeNotice.value = null;
}

/** Price the change first, then let the customer confirm that exact number. */
async function priceThenConfirm({ key, preview, title, describe, start, retry }) {
	busy.value = key;
	actionErr.value = "";
	notice.value = "";
	pending.open = true;
	pending.loading = true;
	pending.title = title;
	pending.amount = "";
	pending.message = "";
	pending.run = null;
	const token = pendingToken;
	try {
		const d = describe(unwrapData(await preview()));
		if (token !== pendingToken) return; // dismissed while we were pricing
		openConfirm({
			title,
			amount: d.amount,
			message: d.message,
			confirmLabel: d.confirmLabel,
			run: () => runPayment({ key, start, retry }),
		});
	} catch (e) {
		closePending();
		// A pricing failure belongs on the page, not in a dialog that just closed.
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

/** start_* then route the answer to the admin-hosted pay page (or fail closed). */
async function runPayment({ key, start, retry, raw = false }) {
	busy.value = key;
	actionErr.value = "";
	notice.value = "";
	codeNotice.value = null; // F11: a stale notice must not sit beside a new error
	try {
		if (raw) {
			// A coded refusal (money parked under review, a rate limit) is an ANSWER
			// about the payment and its row carries the next action - render it rather
			// than reducing it to a red sentence with nowhere to go. A refusal with no
			// code we know is left to its own server message, which for a plan
			// validation ("changing billing cycle is not supported") says more than
			// any generic copy could.
			const verdict = decodePaymentResponse(await start());
			if (!verdict.ok) {
				if (verdict.code && copyFor(verdict.code) !== UNKNOWN_COPY) {
					codeNotice.value = {
						code: verdict.code,
						copy: billingCopyFor(verdict.code),
						retry,
					};
				} else {
					actionErr.value = verdict.message || errMsg(null);
				}
				await loadAccount();
				return;
			}
			await settleWithRedirect(verdict.data, { retry });
			return;
		}
		const data = unwrapData(await start());
		await settleWithRedirect(data, { retry });
	} catch (e) {
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

// F2: what a passive/active Check shows when it finds a live, resumable
// checkout it must not silently navigate to - mirrors onboarding's
// dynamically-added ACTIONS.RESUME ("Continue to payment"), offered instead of
// an automatic top-level navigate.
const RESUMABLE_CHECKOUT_COPY = Object.freeze({
	headline: "You have a payment page open for this subscription.",
	body: "Continue to it to finish, or check again if you're not sure it's still live.",
	actions: [ACTIONS.RESUME, ACTIONS.CHECK],
});

// F4: does this codeless answer carry evidence the payment already settled
// (an Annual downgrade's schedule, a connection payload, a known status) - as
// opposed to an admin answer this build simply could not read?
function looksSettled(d) {
	return !!(
		d.scheduled_plan ||
		d.subscription_status ||
		"tenant_status" in d ||
		"agent_url" in d
	);
}

/**
 * Route a billing action's response (WS8). This page opens NO gateway SDK; it
 * reads the answer and either:
 *
 *   - a code effectiveCode resolves as navigable (F2: computed FIRST, so a
 *     stale token riding a code effectiveCode refuses to override - paid,
 *     terminal, reconnect, money-parked - never navigates): require the
 *     bench's OWN attested origin, then TOP-LEVEL NAVIGATE to
 *     `{origin}/jarvis-checkout#t=…` when `autoNavigate` (the caller already
 *     confirmed a payment); otherwise offer Resume instead of firing the
 *     navigate itself (a status check has confirmed nothing). NO FALLBACK: a
 *     token this site cannot navigate with fails CLOSED with the honest
 *     BENCH_PAY_ORIGIN_UNCONFIGURED copy, never an SDK.
 *   - CLIENT_UPGRADE_REQUIRED: a pre-cutover admin's raw provider handles with no
 *     token. Fail CLOSED with honest copy - the bench opens no sheet for those.
 *   - anything else: render that code's own row (headline + body + its
 *     declared actions) via copyFor when the answer carries a code OR carries
 *     no settlement evidence (F4) - a code this build has never seen, or an
 *     unreadable answer, renders the honest unknown row rather than silence;
 *     a codeless SETTLED answer (an Annual switch just schedules) stays
 *     quiet - then re-read so the page reflects whatever the server actually
 *     settled. F5: a SUCCESS-toned row is dropped if that reload proves the
 *     account is not actually Active - it must never sit above a subscription
 *     the reload just showed as revoked.
 */
async function settleWithRedirect(data, { autoNavigate = true, retry } = {}) {
	const d = data && typeof data === "object" ? data : {};
	codeNotice.value = null;

	const code = effectiveCode({ code: d.code, data: d });

	if (code === CODES.PAYMENT_PAGE_REDIRECT) {
		// Build the URL from a minimal machine-shaped state so the SAME predicate
		// onboarding uses (canNavigateToPay, inside payPageUrl) gates it: a live
		// token, the bench's OWN configured origin, AND admin's attestation that
		// the two agree. Any missing piece returns "" and we fail closed below.
		const url = payPageUrl({
			value: PAY_STATES.UNKNOWN,
			payPageToken: d.pay_page_token,
			payOrigin: d.pay_origin,
			payOriginAttested: d.pay_origin_attested === true,
		});
		if (!url) {
			const copy = copyFor(CODES.BENCH_PAY_ORIGIN_UNCONFIGURED);
			actionErr.value = `${copy.headline} ${copy.body}`;
			await loadAccount();
			return;
		}
		if (autoNavigate) {
			// The copy moment (the same sentence onboarding flashes), then leave
			// top-level in the same tab. bfcache-return re-reads via onPageShow.
			phase.value = "redirect";
			window.location.assign(url);
			return;
		}
		codeNotice.value = { code, copy: RESUMABLE_CHECKOUT_COPY, retry, resumeUrl: url };
		await loadAccount();
		return;
	}

	if (code === CODES.CLIENT_UPGRADE_REQUIRED) {
		const copy = copyFor(CODES.CLIENT_UPGRADE_REQUIRED);
		actionErr.value = `${copy.headline} ${copy.body}`;
		await loadAccount();
		return;
	}

	if (d.code || !looksSettled(d)) {
		codeNotice.value = { code, copy: billingCopyFor(code), retry };
	}
	await loadAccount();

	if (
		codeNotice.value &&
		codeNotice.value.copy.tone === TONE.SUCCESS &&
		account.value.subscription_status !== "Active"
	) {
		codeNotice.value = null;
	}
}

/** F2: straight back to the checkout doCheckStatus already found - no API
 * call, no new intent, just the token/origin its answer already carried. */
function doResumeCheckout() {
	const url = codeNotice.value && codeNotice.value.resumeUrl;
	if (!url) return;
	phase.value = "redirect";
	window.location.assign(url);
}

/** Dispatch one action off the generic notice's row (edge 3: a row with no
 * dedicated branch above still does something real for Check/Initiate; any
 * other declared verb has no billing-page destination of its own yet, so it
 * acknowledges and re-reads rather than leaving a button that does nothing.
 * F6: INITIATE retries whichever flow produced this notice, not always renew. */
function runCodeAction(action) {
	if (action === ACTIONS.CHECK) return doCheckStatus();
	if (action === ACTIONS.RESUME) return doResumeCheckout();
	if (action === ACTIONS.INITIATE) {
		const retry = codeNotice.value && codeNotice.value.retry;
		return retry ? retry() : doRenew();
	}
	if (action === ACTIONS.CONTINUE) {
		// An existing customer who just paid has nothing to "continue" HERE - the
		// setup this action was written for is the signup wizard's. On the billing
		// page the useful next step is the product itself, so send them to chat.
		window.location.assign("/jarvis/");
		return;
	}
	codeNotice.value = null;
	return loadAccount();
}

// The shared vocabulary is written for the SIGNUP wizard, where "we are continuing
// your setup" is true. On this page the customer has an account, a container and a
// plan - they renewed. Same code, different surface, so the surface says its own
// thing rather than borrowing the wizard's.
const BILLING_COPY_OVERRIDES = {
	[CODES.PAYMENT_ALREADY_ACTIVE]: {
		headline: "Payment confirmed.",
		body: "Nothing more is owed - your plan is active.",
		actionLabels: { [ACTIONS.CONTINUE]: "Back to chat" },
	},
	[CODES.BILLING_NO_CURRENT_INTENT]: {
		headline: "No renewal is in progress.",
		body: "Nothing is waiting to be paid on this subscription right now. Renew to extend it.",
	},
};

// The action vocabulary in paymentCodes.js is written for the SIGNUP wizard
// ("Start a new payment", "Check payment status"). On the billing page every
// payment is a RENEWAL of an existing subscription, so the surface renames the
// affordances to say exactly that - without touching the shared labels the
// wizard still relies on. A code's own override (above) wins over these, which
// win over the shared signup defaults.
const BILLING_ACTION_LABELS = {
	[ACTIONS.INITIATE]: "Renew now",
	[ACTIONS.CHECK]: "Check renewal status",
};

function billingCopyFor(code) {
	const base = copyFor(code);
	const over = BILLING_COPY_OVERRIDES[code] || {};
	const actionLabels = {
		...BILLING_ACTION_LABELS,
		...(base.actionLabels || {}),
		...(over.actionLabels || {}),
	};
	return { ...base, ...over, actionLabels };
}
</script>
