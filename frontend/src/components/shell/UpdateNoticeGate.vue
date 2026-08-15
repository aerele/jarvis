<template>
	<!-- Full-screen gate, rendered by AppShell in place of the app. No dismiss. -->
	<div class="jv-gate">
		<div class="jv-gate-bg" aria-hidden="true">
			<div class="jv-gate-orb jv-gate-orb-tl"></div>
			<div class="jv-gate-orb jv-gate-orb-br"></div>
		</div>

		<div class="jv-gate-card">
			<JarvisMark :size="56" :radius="14" class="jv-gate-logo" />

			<span v-if="notice.version" class="jv-gate-badge">Version {{ notice.version }}</span>

			<h1 class="jv-gate-title">A new {{ agentName }} update is available</h1>

			<p v-if="notice.message" class="jv-gate-sub">{{ notice.message }}</p>

			<p class="jv-gate-block">
				Chat with {{ agentName }} is paused for this workspace until it's updated. Please
				ask your administrator to update.
			</p>

			<button class="jv-gate-btn" :disabled="checking" @click="recheck">
				{{ checking ? "Checking…" : "I've updated — check again" }}
			</button>
		</div>
	</div>
</template>

<script setup>
import { onMounted, onBeforeUnmount } from "vue";
import JarvisMark from "@/components/JarvisMark.vue";
import { agentName } from "@/branding";
import { checking, notice, recheck } from "@/noticeGate";

// Boot read a mirror that may predate the update, so re-pull once on mount and
// then poll: an open tab has no other way to learn the notice was lifted.
let timer = null;
onMounted(() => {
	recheck();
	timer = setInterval(recheck, 60000);
});
onBeforeUnmount(() => clearInterval(timer));
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
	margin-bottom: 20px;
}

.jv-gate-badge {
	display: inline-block;
	margin-bottom: 12px;
	padding: 3px 10px;
	border-radius: 999px;
	font-size: 12px;
	font-weight: 600;
	letter-spacing: 0.2px;
	color: #6e5cf6;
	background: rgba(110, 92, 246, 0.12);
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
	margin: 0 0 14px;
	white-space: pre-line;
}

.jv-gate-block {
	font-size: 15px;
	line-height: 1.55;
	font-weight: 500;
	color: var(--ink-gray-8, #2f2f37);
	margin: 0 0 22px;
	max-width: 400px;
}

.jv-gate-btn {
	display: inline-flex;
	align-items: center;
	gap: 8px;
	padding: 10px 26px;
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
.jv-gate-btn:hover:not(:disabled) {
	transform: translateY(-1px);
	box-shadow: 0 10px 24px rgba(110, 92, 246, 0.38);
}
.jv-gate-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
</style>
