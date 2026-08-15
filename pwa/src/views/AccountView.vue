<script setup>
import { computed, onMounted, ref } from "vue";
import { call } from "frappe-ui";
import AppBar from "../components/AppBar.vue";
import * as api from "../api";
import { resetFeed } from "../lib/notifications";

// Account, as the native app has it: who you are, what you're on, what you've
// used, and the way out. No link to the desktop workspace — this IS the app.
const account = ref(null);
const usage = ref(null);
const loading = ref(true);
const signingOut = ref(false);

const user = computed(() => window.frappe_user_id || "");
const fullName = computed(() => window.frappe_full_name || user.value);
const initial = computed(() => (fullName.value || "?").trim().charAt(0).toUpperCase());
const host = computed(() => window.location.host);

// get_account proxies the admin's summary: {ok, data:{subscription_status,
// plan:{plan_name, billing_cycle, price_inr}, current_period_end, autorenew}}.
const acc = computed(() => account.value?.data ?? account.value ?? {});
const plan = computed(() => acc.value.plan || null);
const status = computed(() => acc.value.subscription_status || "none");
const active = computed(() => status.value === "Active");

const planLabel = computed(() => {
	const p = plan.value;
	if (!p) return "No subscription";
	const cycle =
		p.billing_cycle === "Yearly" ? "/yr" : p.billing_cycle === "Monthly" ? "/mo" : "";
	const price = p.price_inr ? ` · ₹${Number(p.price_inr).toLocaleString("en-IN")}${cycle}` : "";
	return `${p.plan_name}${price}`;
});

function fmtDate(v) {
	if (!v) return "—";
	const d = new Date(String(v).replace(" ", "T"));
	return Number.isNaN(d.getTime())
		? String(v)
		: d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function fmtTokens(n) {
	if (n == null) return "—";
	if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
	if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
	return String(n);
}

const used = computed(() => usage.value?.month_tokens ?? 0);
const budget = computed(() => usage.value?.budget_monthly ?? 0);
const pct = computed(() =>
	budget.value > 0 ? Math.min(100, Math.round((used.value / budget.value) * 100)) : 0
);

async function signOut() {
	if (signingOut.value) return;
	signingOut.value = true;
	try {
		await call("logout");
	} catch {
		// Session may already be gone; leave for the login screen either way.
	}
	// The feed is this person's task history — the next user of this browser must
	// not inherit it, or its unread badge.
	resetFeed();
	// In-scope: an installed app must not end its session by throwing the user
	// into a browser tab.
	window.location.href = "/jarvis-mobile/login";
}

onMounted(async () => {
	// Neither call is essential to the screen, so a failure in one must not blank
	// the other — settle both.
	const [a, u] = await Promise.allSettled([api.getAccount(), api.getUsage()]);
	if (a.status === "fulfilled") account.value = a.value;
	if (u.status === "fulfilled") usage.value = u.value;
	loading.value = false;
});
</script>

<template>
	<AppBar title="Account" />

	<div class="jv-scroll jv-pad">
		<div class="jv-id">
			<div class="jv-avatar">{{ initial }}</div>
			<div class="jv-id-text">
				<div class="jv-id-name">{{ fullName }}</div>
				<div class="jv-id-host">{{ user }}</div>
			</div>
		</div>

		<div v-if="loading" class="jv-skel">
			<div />
			<div />
			<div />
		</div>

		<template v-else>
			<div class="jv-card">
				<div class="jv-row">
					<span>Plan</span>
					<strong>{{ planLabel }}</strong>
				</div>
				<div class="jv-row">
					<span>Status</span>
					<span class="jv-status" :class="active ? 'is-ok' : 'is-warn'">
						<span class="jv-dot" />
						{{ status === "none" ? "Not subscribed" : status }}
					</span>
				</div>
				<div class="jv-row is-last">
					<span>{{ acc.autorenew ? "Renews" : "Valid till" }}</span>
					<strong class="jv-num">{{ fmtDate(acc.current_period_end) }}</strong>
				</div>
			</div>

			<div class="jv-card jv-usage">
				<div class="jv-usage-head">
					<span>Usage this month</span>
					<span class="jv-num">
						{{
							budget > 0
								? `${fmtTokens(used)} / ${fmtTokens(budget)} tokens`
								: `${fmtTokens(used)} tokens`
						}}
					</span>
				</div>
				<div class="jv-track"><div class="jv-fill" :style="{ width: `${pct}%` }" /></div>
				<div class="jv-usage-foot">
					{{
						[usage?.month_label, usage?.estimated ? "estimated" : null]
							.filter(Boolean)
							.join(" · ") || "—"
					}}
				</div>
			</div>

			<p class="jv-note">
				Jarvis works with your permissions. It can only see and change what you can.
			</p>

			<button class="jv-signout" :disabled="signingOut" @click="signOut">
				<svg
					viewBox="0 0 24 24"
					width="17"
					height="17"
					fill="none"
					stroke="currentColor"
					stroke-width="1.9"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
				</svg>
				{{ signingOut ? "Signing out…" : "Sign out" }}
			</button>
		</template>
	</div>
</template>

<style scoped>
.jv-pad {
	padding: 6px 16px 40px;
}
.jv-id {
	display: flex;
	align-items: center;
	gap: 13px;
	padding: 6px 2px 20px;
}
.jv-avatar {
	display: grid;
	place-items: center;
	/* Geometry from main's avatar redesign (52px rounded square). */
	width: 52px;
	height: 52px;
	flex: none;
	border-radius: 14px;
	/* The user's avatar, not the brand mark — but it was carrying a THIRD,
	   independently hand-written violet gradient (#8b7cf7→#6a56e8). One brand
	   gradient per product. */
	background: var(--brand-grad);
	color: #fff;
	font-size: 20px;
	font-weight: 600;
}
.jv-id-text {
	flex: 1;
	min-width: 0;
}
.jv-id-name {
	font-size: 16px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-id-host {
	font-size: 12.5px;
	color: var(--ink5);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.jv-card {
	margin-bottom: 16px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-row {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	padding: 13px;
	border-bottom: 1px solid var(--border);
	font-size: 13px;
	color: var(--ink5);
}
.jv-row.is-last {
	border-bottom: 0;
}
.jv-row strong {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-num {
	font-variant-numeric: tabular-nums;
}
.jv-status {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	font-size: 12.5px;
	font-weight: 600;
}
.jv-status.is-ok {
	color: var(--green);
}
.jv-status.is-warn {
	color: var(--amber);
}
.jv-dot {
	width: 7px;
	height: 7px;
	border-radius: 999px;
	background: currentColor;
}

.jv-usage {
	padding: 14px;
}
.jv-usage-head {
	display: flex;
	align-items: baseline;
	justify-content: space-between;
	gap: 10px;
	margin-bottom: 9px;
	font-size: 13px;
	color: var(--ink6);
}
.jv-usage-head span:last-child {
	font-size: 12.5px;
	color: var(--ink5);
}
.jv-track {
	height: 9px;
	border-radius: 999px;
	background: var(--card3);
	overflow: hidden;
}
.jv-fill {
	height: 100%;
	border-radius: 999px;
	background: var(--accent);
	transition: width 0.3s ease;
}
.jv-usage-foot {
	margin-top: 8px;
	font-size: 11.5px;
	color: var(--ink4);
}

.jv-note {
	margin: 0 0 20px;
	padding: 12px 14px;
	border-radius: 10px;
	background: var(--accent-bg);
	color: var(--ink7);
	font-size: 12.5px;
	line-height: 1.5;
	text-align: center;
}
.jv-signout {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 8px;
	width: 100%;
	height: 46px;
	border: 1px solid var(--red);
	border-radius: 12px;
	background: transparent;
	color: var(--red);
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-signout:disabled {
	opacity: 0.6;
}

.jv-skel > div {
	height: 72px;
	margin-bottom: 12px;
	border-radius: 12px;
	background: var(--card2);
	animation: jv-pulse 1.4s ease-in-out infinite;
}
@keyframes jv-pulse {
	0%,
	100% {
		opacity: 0.55;
	}
	50% {
		opacity: 1;
	}
}
</style>
