<template>
	<SettingsPane
		title="Plan and billing"
		description="Your subscription, renewal and upgrade options."
		:error="accountErr"
	>
		<p v-if="accountLoading" class="text-p-base text-ink-gray-6">Loading…</p>

		<!-- The error copy itself rides SettingsPane's :error prop; only the
		     recovery control lives here, so retry is one pattern across the whole
		     settings surface (design.md §5 anti-pattern 6). -->
		<div v-else-if="accountErr">
			<Button
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="accountLoading"
				@click="loadAccount"
			/>
		</div>

		<template v-else>
			<p v-if="!account.plan || !account.plan.plan_name" class="text-p-base text-ink-gray-6">
				No active plan yet.
			</p>

			<template v-else>
				<!-- current plan: name, price, status badge on one line -->
				<div class="flex flex-wrap items-center gap-3">
					<span class="text-base font-medium text-ink-gray-8">
						{{ account.plan.plan_name }}
					</span>
					<span class="text-base text-ink-gray-6">
						{{ planPriceLabel(account.plan.price_inr, account.plan.billing_cycle) }}
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
					}}<template v-if="account.autorenew && !cancelling"> · Auto-renew on</template>
				</p>

				<!-- Scheduled cancellation: state it plainly and put Resume right
				     here, so the one affordance that undoes it is where the customer
				     is already looking. This is the pane's single solid button. -->
				<div
					v-if="cancelling"
					class="mt-4 flex items-center justify-between gap-4 rounded-md border p-4"
				>
					<span class="text-p-sm text-ink-gray-7">
						{{ cancellationNotice(account.access_ends_on) }}
					</span>
					<Button variant="solid" label="Resume" :loading="busy" @click="doResume" />
				</div>

				<ul v-if="features.length" class="mt-4 flex flex-col gap-2">
					<li
						v-for="(f, i) in features"
						:key="i"
						class="flex items-center gap-2 text-p-sm text-ink-gray-7"
					>
						<FeatherIcon name="check" class="size-4 shrink-0 text-ink-gray-5" />
						<span>{{ f }}</span>
					</li>
				</ul>
			</template>

			<!-- A downgrade already scheduled: state it plainly, and put the one
			     affordance that undoes it right here, mirroring the cancellation
			     notice above. Undoing an Annual switch is a plain flag the server
			     clears, so it happens inline. A Monthly one already migrated the
			     mandate, so undoing it needs a Razorpay checkout, which this
			     summary pane does not host - the billing page does.
			     Subtle, never solid: Resume above is the pane's single solid
			     button. -->
			<div
				v-if="scheduledDowngrade"
				class="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border p-4"
			>
				<span class="text-p-sm text-ink-gray-7">{{ scheduledDowngradeNotice }}</span>
				<Button
					variant="subtle"
					label="Keep current plan"
					:loading="busy"
					@click="
						account.scheduled_downgrade_revocable ? doCancelDowngrade() : goBilling()
					"
				/>
			</div>

			<!-- Autopay off but re-armable. This MUST carry an action: a released
			     mandate is terminal at Razorpay, so neither resume nor a one-shot
			     renew brings auto-renewal back, and the notice alone left the
			     customer told to "set up payment again" with nothing to click.
			     Re-arming means a mandate checkout, so it hands off to the page. -->
			<div
				v-if="account.can_reauthorize"
				class="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border p-4"
			>
				<span class="text-p-sm text-ink-gray-7">{{ reauthBanner }}</span>
				<Button variant="subtle" label="Set up auto-renewal" @click="goBilling" />
			</div>
			<p
				v-else-if="reauthNotice"
				class="mt-4 rounded-md border p-4 text-p-sm text-ink-gray-7"
			>
				{{ reauthNotice }}
			</p>

			<hr class="my-8" />

			<!-- Manage footer. This pane is a SUMMARY: it keeps the state and the
			     three in-place actions that need no payment (cancel, resume, undo
			     a revocable switch), and every action that takes money lives on
			     the billing page. The plan-comparison grids that used to sit here
			     went with them - showing plan cards in both places is how the two
			     surfaces drift apart.

			     Cancel is a red SUBTLE button, never red solid: the confirm dialog
			     owns the deliberate red step, and a solid red resting on the pane
			     just makes it hostile (design.md §4.1 danger zone). Hidden while
			     cancelling (Resume is above) or ended (nothing left to cancel). -->
			<div class="flex items-center justify-between gap-4">
				<Button
					variant="solid"
					:label="ended ? 'Renew subscription' : 'Manage plan and billing'"
					iconRight="arrow-right"
					@click="goBilling"
				/>
				<Button
					v-if="!cancelling && !ended"
					variant="subtle"
					theme="red"
					:label="cancelActionLabel(account.has_mandate)"
					:loading="busy"
					@click="doCancel"
				/>
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { Badge, Button, FeatherIcon } from "frappe-ui";
import { getAccount, cancelPlanAtPeriodEnd, resumePlan, cancelScheduledDowngrade } from "@/api";
import {
	statusLabel,
	statusBadgeTheme,
	planPriceLabel,
	planFeatures,
	renewalLabel,
	cancelActionLabel,
	cancelPillLabel,
	cancellationNotice,
} from "@/account/format.js";
import { useConfirm } from "@/composables/useConfirm";
import { useShellStore } from "@/stores/shell";
import { errMessage as errMsg } from "@/lib/errors";
import SettingsPane from "@/components/settings/SettingsPane.vue";

const { confirm } = useConfirm();
const router = useRouter();
const store = useShellStore();

// Billing is a full page now, not a Desk trip. Closing the dialog first matters:
// routing underneath an open modal leaves the customer looking at settings on
// top of the page they asked for.
function goBilling() {
	store.settingsOpen = false;
	router.push({ name: "Billing" });
}

const account = ref({});
const accountLoading = ref(true);
const accountErr = ref("");

// Shared with the billing page's cards via account/format.js, so the two
// surfaces cannot disagree about what a plan includes.
const features = computed(() => planFeatures(account.value.plan));
const scheduledDowngrade = computed(() => !!account.value.scheduled_plan);
const scheduledDowngradeNotice = computed(() => {
	const name =
		account.value.scheduled_plan_name || account.value.scheduled_plan || "a smaller plan";
	const on = (account.value.scheduled_plan_on || "").split(" ")[0];
	return on
		? `Switching to ${name} on ${on}. You keep your current plan until then.`
		: `Switching to ${name} at your next billing cycle.`;
});
// A plan scheduled to end. Server keeps status "Active" through the paid
// period, so this flag - not the status - drives the cancelling UI.
const cancelling = computed(() => !!account.value.cancel_at_period_end);
// Terminal: paid period over, no access left to cancel and no Resume - only a
// fresh payment restores service. Distinct from `cancelling` (still entitled).
const ENDED_STATUSES = new Set(["Expired", "Cancelled"]);
const ended = computed(() => ENDED_STATUSES.has(account.value.subscription_status));
const statusTheme = computed(() =>
	statusBadgeTheme(account.value.subscription_status, cancelling.value)
);
const busy = ref(false);
const reauthNotice = ref("");

async function loadAccount() {
	accountLoading.value = true;
	accountErr.value = "";
	try {
		account.value = (await getAccount()) || {};
	} catch (e) {
		accountErr.value = errMsg(e);
	} finally {
		accountLoading.value = false;
	}
}

async function doCancel() {
	const label = cancelActionLabel(account.value.has_mandate);
	const endsOn = (account.value.access_ends_on || "").split(" ")[0];
	const ok = await confirm({
		title: `${label}?`,
		message: endsOn
			? `You'll keep full access until ${endsOn}. You can resume any time before then.`
			: "You'll keep full access until the end of your current period, and can resume any time before then.",
		confirmLabel: label,
		danger: true,
	});
	if (!ok) return;
	busy.value = true;
	accountErr.value = "";
	reauthNotice.value = "";
	try {
		await cancelPlanAtPeriodEnd();
		// Re-read rather than optimistically patching: the server payload is
		// the truth, and it is a single round-trip.
		await loadAccount();
	} catch (e) {
		accountErr.value = errMsg(e);
	} finally {
		busy.value = false;
	}
}

// Derived from the server payload, not from the resume response alone, so the
// banner survives a page reload (the pane is reopened far more often than a
// resume is performed).
const reauthBanner = computed(() => {
	const endsOn = (account.value.access_ends_on || "").split(" ")[0];
	return endsOn
		? `Auto-renewal is off. Set it up before ${endsOn} to stay subscribed.`
		: "Auto-renewal is off. Set it up before your period ends.";
});

async function doResume() {
	// Constructive action - no danger confirm.
	busy.value = true;
	accountErr.value = "";
	try {
		const out = (await resumePlan()) || {};
		if (out.requires_reauthorization) {
			// Cancelling released the autopay mandate and there is no way to
			// re-arm it silently; say so rather than let them assume it renews.
			const endsOn = (account.value.access_ends_on || "").split(" ")[0];
			reauthNotice.value = endsOn
				? `Auto-renewal is off. Set up payment again before ${endsOn} to stay subscribed.`
				: "Auto-renewal is off. Set up payment again before your period ends.";
		}
		await loadAccount();
	} catch (e) {
		accountErr.value = errMsg(e);
	} finally {
		busy.value = false;
	}
}

async function doCancelDowngrade() {
	// Constructive, so no danger confirm. Only offered when the server says the
	// scheduled downgrade is revocable (no committed mandate migration).
	busy.value = true;
	accountErr.value = "";
	try {
		await cancelScheduledDowngrade();
		await loadAccount();
	} catch (e) {
		accountErr.value = errMsg(e);
	} finally {
		busy.value = false;
	}
}

onMounted(loadAccount);
</script>
