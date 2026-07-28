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

		<!-- Confirm step. The customer sees the exact amount HERE, before any
		     payment sheet exists.

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

		<!-- Page-blocking wait. Covers the whole surface while the sheet is open
		     and while verification is in flight, so nothing behind it can be
		     clicked into an inconsistent state. `phase` is cleared in a finally,
		     so no path can leave the page stuck here. -->
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
 * reauthorize were all already whitelisted for the desk page. The only thing
 * that lived exclusively in Desk was Razorpay Checkout, which is now
 * @/lib/useRazorpay. This page owns the copy and the choreography; the pay-then
 * -apply half is @/lib/billingCheckout.
 */
import { ref, reactive, computed, onMounted } from "vue";
import { Badge, Breadcrumbs, Button, Dialog, ErrorMessage, toast } from "frappe-ui";
import * as api from "@/api";
import LayoutHeader from "@/components/LayoutHeader.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import PlanCard from "./PlanCard.vue";
import BillingNotice from "./BillingNotice.vue";
import { useJarvisTheme } from "@/theme";
import { errMessage as errMsg } from "@/lib/errors";
import { openCheckout } from "@/lib/useRazorpay";
import {
	payAndApply,
	accountSnapshot,
	unwrapData,
	PAY_APPLIED,
	PAY_DISMISSED,
	PAY_PENDING,
} from "@/lib/billingCheckout";
import {
	inr,
	statusLabel,
	pillTone,
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
// "" | "sheet" | "verifying" | "applying" - drives the blocking overlay.
const phase = ref("");

const PHASE_LABELS = {
	sheet: "Waiting for your payment…",
	verifying: "Confirming your payment…",
	applying: "Updating your plan…",
};
const phaseLabel = computed(() => PHASE_LABELS[phase.value] || "Working…");

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

const PILL_THEME = {
	"jv-pill-ok": "green",
	"jv-pill-warn": "orange",
	"jv-pill-bad": "red",
	"jv-pill-muted": "gray",
};
const statusTheme = computed(
	() => PILL_THEME[pillTone(account.value.subscription_status, cancelling.value)] || "gray"
);

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
onMounted(loadAccount);

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
		description: "Plan upgrade",
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
		description: "Plan change",
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
				description: "Subscription renewal",
			}),
	});
}

async function doReauthorize() {
	openConfirm({
		title: "Set up auto-renewal",
		amount: "",
		// Mandate-only checkout. Saying "nothing is charged" is load-bearing: the
		// sheet still shows a payment form and looks like it will take money.
		message:
			"You will confirm a payment method. Nothing is charged now - your current period is already paid for.",
		confirmLabel: "Continue",
		run: () =>
			runPayment({
				key: "reauth",
				start: () => api.reauthorizeAutopay(),
				description: "Auto-renewal setup",
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
		const handles = unwrapData(await api.cancelScheduledDowngrade());
		// Monthly: revoking the switch also dropped the cheaper mandate, so the
		// current plan's mandate has to be re-armed in the same step. Annual
		// returns nothing to pay and falls through to a plain reload.
		await settleWithCheckout(handles, "Keep current plan");
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
async function priceThenConfirm({ key, preview, title, describe, start, description }) {
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
			run: () => runPayment({ key, start, description }),
		});
	} catch (e) {
		closePending();
		// A pricing failure belongs on the page, not in a dialog that just closed.
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

/** start_* then Checkout then verify then wait for the plan to move. */
async function runPayment({ key, start, description }) {
	busy.value = key;
	actionErr.value = "";
	notice.value = "";
	try {
		const handles = unwrapData(await start());
		await settleWithCheckout(handles, description);
	} catch (e) {
		actionErr.value = errMsg(e);
	} finally {
		busy.value = "";
	}
}

/**
 * Hand `handles` to Checkout and reconcile the outcome.
 * Every exit clears `phase`, which is what keeps the overlay from stranding
 * the page when the sheet closes by any route.
 */
async function settleWithCheckout(handles, description) {
	const before = accountSnapshot(account.value);
	let out;
	try {
		out = await payAndApply({
			handles,
			description,
			before,
			openCheckout,
			finishPayment: api.finishPayment,
			getAccount: api.getAccount,
			onPhase: (p) => {
				phase.value = p;
			},
		});
	} finally {
		phase.value = "";
	}

	if (out.status === PAY_DISMISSED) {
		// Explicitly nothing: no toast, no error, no reload. The customer closed
		// the sheet and the page must look exactly as they left it.
		return;
	}
	if (out.status === PAY_APPLIED) {
		account.value = out.account || account.value;
		// Success is a toast, never an inline green block (design.md anti-pattern 16).
		toast.success("Your plan is updated.");
		notice.value = out.verified
			? ""
			: "Payment received and your plan is updated. Our confirmation step did not respond, but the change has landed.";
		return;
	}
	if (out.status === PAY_PENDING) {
		// Money moved but the plan had not flipped before we stopped waiting.
		// Distinct from a dismissal and distinct from a failure: say what is
		// true and do not pretend it failed.
		notice.value =
			"Your payment was received. Your plan should update within a minute - refresh to check, or contact support if it does not.";
		await loadAccount();
		return;
	}
	// Nothing to pay: the server settled it (an annual switch just schedules).
	await loadAccount();
}
</script>
