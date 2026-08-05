<template>
	<!-- Full-screen onboarding gate (D11-safe): shown by AppShell IN PLACE OF the
	     sidebar + routed page whenever the workspace hasn't finished onboarding.
	     No app chrome — a not-yet-connected workspace has nothing to navigate to.
	     A rendered gate, never a redirect, so it can't reintroduce the old
	     desk↔SPA onboarding loop.

	     No decorative background orbs or gradient CTA here (design.md §4.3 CTA
	     discipline / §5 anti-pattern 4): the brand-asset exception in §2.2 covers
	     the Jarvis mark's own gradient and the onboarding "processing" canvas,
	     not a page-level poster background. -->
	<div class="flex flex-1 items-center justify-center overflow-hidden bg-surface-white p-6">
		<div class="flex w-full max-w-md flex-col items-center text-center">
			<JarvisMark :size="56" :radius="14" class="mb-6" />

			<h1 class="mb-2.5 text-2xl font-semibold leading-tight text-ink-gray-9">
				{{ disconnected ? `Reconnect ${agentName}` : `Finish setting up ${agentName}` }}
			</h1>

			<!-- MAJOR 1 fix: showGate covers a disconnected bench too - a cleared
			     admin connection makes is_ready_for_chat return "signup", same as
			     a first-time bench - and this gate is the only screen it lands on;
			     every openSettings() call site (GeneralPane included) lives in the
			     unrendered subtree behind showGate. The two branches below are
			     byte-identical to the pre-T16 copy so a not-disconnected (or
			     failed-call) bench renders exactly as it did before. -->
			<p v-if="!disconnected && isSystemManager" class="mb-7 text-p-base text-ink-gray-6">
				This workspace isn't connected to an AI agent yet. Complete a short setup to start
				chatting with {{ agentName }} about your ERPNext data.
			</p>
			<p v-else-if="!disconnected" class="mb-7 text-p-base text-ink-gray-6">
				{{ agentName }} isn't set up for this workspace yet. Please ask your administrator
				(a System Manager) to complete onboarding.
			</p>
			<p v-else class="mb-7 text-p-base text-ink-gray-6">{{ recoveryMessage }}</p>

			<Button
				v-if="isSystemManager"
				variant="solid"
				size="lg"
				:label="disconnected ? 'Reconnect' : 'Complete setup'"
				iconRight="arrow-right"
				@click="goOnboard"
			/>

			<!-- Onboarding footer-nav convention (design.md §4.3): a secondary path
			     under the card is plain muted text, not a Button. -->
			<button
				type="button"
				class="mt-3.5 text-p-sm text-ink-gray-5 hover:text-ink-gray-7"
				@click="switchToDesk"
			>
				Switch to Desk
			</button>
		</div>
	</div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Button } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { agentName } from "@/branding";
import { benchConnectionState } from "@/api";

const router = useRouter();

// The /onboarding route's beforeEnter gate is a STRICT truthy check
// (`(is_system_manager || is_jarvis_admin) ? … : Chat`). Match it exactly here —
// a lenient `!== false` would show the "Complete setup" button on the vite dev
// server (flags undefined), but the click would bounce straight back off the
// route guard to Chat and re-trigger the poster: a dead-end. So the button
// appears only when it can actually reach the wizard; a non-admin (or dev) user
// gets the "ask your administrator" copy instead. PART 4 REVISED TASK 49(c):
// widened to the Jarvis Admin tenant-admin tier.
const isSystemManager = !!(window.is_system_manager || window.is_jarvis_admin);

// MAJOR 1: a disconnected bench (L4 reset, or the separate "Disconnect this
// bench" action) also lands here — is_ready_for_chat returns "signup" for it,
// same as a workspace that never onboarded, and this gate replaces the whole
// app for both. Defaults below keep the poster generic until proven otherwise.
const disconnected = ref(false);
const needsCompany = ref(false);

onMounted(async () => {
	// bench_connection_state() reads local settings only and makes no admin
	// call, so it still answers on a disconnected bench even though that bench
	// holds no admin credentials — the same property GeneralPane.vue's
	// resumeResetIfInFlight relies on. A thrown error must leave `disconnected`
	// at its false default: a first-time bench must never be told it was
	// disconnected.
	try {
		const state = (await benchConnectionState()) || {};
		disconnected.value = state.disconnected === true;
		needsCompany.value = state.needs_company === true;
	} catch (e) {
		// Local endpoint unavailable — fall through to the generic poster
		// rather than guessing at a disconnected state.
	}
});

// Mirrors GeneralPane.vue's disconnectRecoveryText: same emailed-code
// reconnect, same "needs_company" clause, worded for whichever role is
// looking at this gate — the admin who can act on it via the Button below,
// or the teammate who can only relay it to one.
const recoveryMessage = computed(() => {
	const base = isSystemManager
		? "This workspace still exists — it's just disconnected from your account. Reconnect with the one-time code emailed to this workspace's registered address."
		: "This workspace still exists — it's just disconnected from your account. Ask your administrator (a System Manager) to reconnect it with the one-time code emailed to this workspace's registered address.";
	if (!needsCompany.value) return base;
	const companyClause = isSystemManager
		? " That address is linked to more than one company, so you'll also need to give the company name."
		: " That address is linked to more than one company, so they'll also need to give the company name.";
	return base + companyClause;
});

function goOnboard() {
	router.push({ name: "Onboarding" });
}

function switchToDesk() {
	window.location.href = "/app";
}
</script>
