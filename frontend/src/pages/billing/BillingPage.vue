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
				<Breadcrumbs :items="[{ label: 'Plan and billing' }]" />
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
						<h1 class="text-2xl font-semibold text-ink-gray-9">Plan and billing</h1>
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
					</div>

					<p v-if="changesBlockedReason" class="mt-3 text-p-sm text-ink-gray-6">
						{{ changesBlockedReason }}
					</p>
					<p
						v-if="!upgradePlans.length && !downgradePlans.length"
						class="mt-4 text-p-sm text-ink-gray-6"
					>
						There are no other plans available on your account right now.
					</p>

					<ErrorMessage v-if="actionErr" class="mt-6" :message="actionErr" />
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
import { ref, reactive, computed, onMounted, onBeforeUnmount } from "vue";
import { Badge, Breadcrumbs, Button, Dialog, ErrorMessage } from "frappe-ui";
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
import { effectiveCode } from "@/onboarding/paymentCodec";
import { CODES, copyFor } from "@/onboarding/paymentCodes";
import { payPageUrl, STATES as PAY_STATES } from "@/onboarding/paymentMachine";
import {
	inr,
	statusLabel,
	statusBadgeTheme,
	planPriceLabel,
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
const ENDED_STATUSES = new Set(["Expired", "Cancelled"]);
const ended = computed(() => ENDED_STATUSES.has(account.value.subscription_status));

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
	if (ended.value) return { label: "Renew", note: "Renewing restores access straight away." };
	if (cancelling.value) return { label: "", note: "Resume above to keep this plan." };
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
onMounted(() => {
	loadAccount();
	window.addEventListener("pageshow", onPageShow);
});
onBeforeUnmount(() => window.removeEventListener("pageshow", onPageShow));

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
		describe: (d) => ({
			amount: inr(d.prorated_inr),
			message:
				"Charged now for the days left in your current billing period. Your new plan starts immediately.",
			confirmLabel: `Pay ${inr(d.prorated_inr)}`,
		}),
		start: () => api.startUpgrade(plan.name),
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
	});
}

async function doRenew() {
	const price = currentPlan.value ? currentPlan.value.price_inr : 0;
	// Renew has no preview_* sibling: the amount is simply the plan's price, so
	// the confirm step reads it off the plan rather than minting an order the
	// customer has not agreed to yet.
	openConfirm({
		title: "Renew subscription",
		amount: inr(price),
		message: "Renewing restores access straight away for another full billing period.",
		confirmLabel: `Pay ${inr(price)}`,
		run: () =>
			runPayment({
				key: "renew",
				start: () => api.renewPlan(),
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

async function doKeepCurrentPlan() {
	busy.value = "keep";
	actionErr.value = "";
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
		await settleWithRedirect(data);
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
}

/** Price the change first, then let the customer confirm that exact number. */
async function priceThenConfirm({ key, preview, title, describe, start }) {
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
			run: () => runPayment({ key, start }),
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
async function runPayment({ key, start }) {
	busy.value = key;
	actionErr.value = "";
	notice.value = "";
	try {
		const data = unwrapData(await start());
		await settleWithRedirect(data);
	} catch (e) {
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

/**
 * Route a billing action's response (WS8). This page opens NO gateway SDK; it
 * reads the answer with onboarding's own `effectiveCode` and either:
 *
 *   - PAYMENT_PAGE_REDIRECT: a live pay-page token. Require the bench's OWN
 *     attested origin, then TOP-LEVEL NAVIGATE to `{origin}/jarvis-checkout#t=…`
 *     (payPageUrl + window.location.assign) - the exact mechanism
 *     usePaymentFlow.navigateToPay uses. NO FALLBACK: a token this site cannot
 *     navigate with fails CLOSED with the honest BENCH_PAY_ORIGIN_UNCONFIGURED
 *     copy, never an SDK.
 *   - CLIENT_UPGRADE_REQUIRED: a pre-cutover admin's raw provider handles with no
 *     token. Fail CLOSED with honest copy - the bench opens no sheet for those.
 *   - anything else: nothing to pay. The server already settled it (an Annual
 *     downgrade just schedules), so re-read and show what actually happened.
 */
async function settleWithRedirect(data) {
	const d = data || {};
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
		// The copy moment (the same sentence onboarding flashes), then leave
		// top-level in the same tab. bfcache-return re-reads via onPageShow.
		phase.value = "redirect";
		window.location.assign(url);
		return;
	}

	if (code === CODES.CLIENT_UPGRADE_REQUIRED) {
		const copy = copyFor(CODES.CLIENT_UPGRADE_REQUIRED);
		actionErr.value = `${copy.headline} ${copy.body}`;
		await loadAccount();
		return;
	}

	// Nothing to pay: the server settled it (an annual switch just schedules, or
	// the change already applied). Re-read and let the page reflect the new state.
	await loadAccount();
}
</script>
