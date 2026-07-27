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
				Finish setting up {{ agentName }}
			</h1>

			<p v-if="isSystemManager" class="mb-7 text-p-base text-ink-gray-6">
				This workspace isn't connected to an AI agent yet. Complete a short setup to start
				chatting with {{ agentName }} about your ERPNext data.
			</p>
			<p v-else class="mb-7 text-p-base text-ink-gray-6">
				{{ agentName }} isn't set up for this workspace yet. Please ask your administrator
				(a System Manager) to complete onboarding.
			</p>

			<Button
				v-if="isSystemManager"
				variant="solid"
				size="lg"
				label="Complete setup"
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
import { useRouter } from "vue-router";
import { Button } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { agentName } from "@/branding";

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

function goOnboard() {
	router.push({ name: "Onboarding" });
}

function switchToDesk() {
	window.location.href = "/app";
}
</script>
