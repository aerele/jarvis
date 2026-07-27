<template>
	<!-- Full-screen onboarding gate (D11-safe): shown by AppShell IN PLACE OF the
	     sidebar + routed page whenever the workspace hasn't finished onboarding.
	     No app chrome — a not-yet-connected workspace has nothing to navigate to.
	     A rendered gate, never a redirect, so it can't reintroduce the old
	     desk↔SPA onboarding loop. -->
	<div class="jv-gate">
		<!-- Decorative framing orbs (mirrors OnboardingView) — aria-hidden, no
		     pointer events; quiet brand tint that reads on light and dark. -->
		<div class="jv-gate-bg" aria-hidden="true">
			<div class="jv-gate-orb jv-gate-orb-tl"></div>
			<div class="jv-gate-orb jv-gate-orb-br"></div>
		</div>

		<div class="jv-gate-card">
			<JarvisMark :size="56" :radius="14" class="jv-gate-logo" />

			<h1 class="jv-gate-title">Finish setting up {{ agentName }}</h1>

			<p v-if="isSystemManager" class="jv-gate-sub">
				This workspace isn't connected to an AI agent yet. Complete a short setup to start
				chatting with {{ agentName }} about your ERPNext data.
			</p>
			<p v-else class="jv-gate-sub">
				{{ agentName }} isn't set up for this workspace yet. Please ask your administrator
				(a System Manager) to complete onboarding.
			</p>

			<button v-if="isSystemManager" class="jv-gate-btn" @click="goOnboard">
				Complete setup
				<span class="jv-gate-arrow" aria-hidden="true">→</span>
			</button>

			<button class="jv-gate-ghost" @click="switchToDesk">Switch to Desk</button>
		</div>
	</div>
</template>

<script setup>
import { useRouter } from "vue-router";
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

<style scoped>
.jv-gate {
	position: relative;
	flex: 1;
	display: grid;
	place-items: center;
	width: 100%;
	height: 100%;
	overflow: hidden;
	background: var(--surface-white, #fff);
	padding: 24px;
}

.jv-gate-bg {
	position: absolute;
	inset: 0;
	pointer-events: none;
}
.jv-gate-orb {
	position: absolute;
	width: 460px;
	height: 460px;
	border-radius: 50%;
	filter: blur(40px);
	opacity: 0.5;
}
.jv-gate-orb-tl {
	top: -160px;
	left: -160px;
	background: radial-gradient(circle at 30% 30%, rgba(110, 139, 255, 0.28), transparent 70%);
}
.jv-gate-orb-br {
	bottom: -180px;
	right: -160px;
	background: radial-gradient(circle at 70% 70%, rgba(139, 92, 246, 0.24), transparent 70%);
}

.jv-gate-card {
	position: relative;
	z-index: 1;
	display: flex;
	flex-direction: column;
	align-items: center;
	text-align: center;
	max-width: 440px;
	width: 100%;
}

.jv-gate-logo {
	box-shadow: 0 8px 24px rgba(110, 92, 246, 0.28);
	margin-bottom: 24px;
}

.jv-gate-title {
	font-size: 24px;
	font-weight: 600;
	line-height: 1.25;
	color: var(--ink-gray-9, #171717);
	margin: 0 0 10px;
}

.jv-gate-sub {
	font-size: 15px;
	line-height: 1.55;
	color: var(--ink-gray-6, #6b7280);
	margin: 0 0 28px;
}

.jv-gate-btn {
	display: inline-flex;
	align-items: center;
	gap: 8px;
	padding: 10px 22px;
	border: none;
	border-radius: 9px;
	font-size: 15px;
	font-weight: 500;
	color: #fff;
	cursor: pointer;
	background: linear-gradient(135deg, #6e8bff, #8b5cf6);
	box-shadow: 0 6px 18px rgba(110, 92, 246, 0.3);
	transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.jv-gate-btn:hover {
	transform: translateY(-1px);
	box-shadow: 0 10px 24px rgba(110, 92, 246, 0.38);
}
.jv-gate-btn:active {
	transform: translateY(0);
}
.jv-gate-arrow {
	font-size: 16px;
	line-height: 1;
}

.jv-gate-ghost {
	margin-top: 14px;
	padding: 6px 12px;
	border: none;
	background: transparent;
	font-size: 13px;
	color: var(--ink-gray-5, #9ca3af);
	cursor: pointer;
	border-radius: 7px;
	transition: color 0.15s ease;
}
.jv-gate-ghost:hover {
	color: var(--ink-gray-7, #4b5563);
}
</style>
