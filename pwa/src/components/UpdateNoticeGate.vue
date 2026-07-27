<script setup>
import { onMounted, onBeforeUnmount } from "vue";
import BrandMark from "./BrandMark.vue";
import { agentName } from "@/branding";
import { checking, notice, recheck } from "../noticeGate";

// The PWA never calls the chat-readiness gate, so this is its only way to learn
// the notice was lifted short of the daily sync.
let timer = null;
onMounted(() => {
	recheck();
	timer = setInterval(recheck, 60000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
	<!-- Full-screen overlay above the app. No dismiss. -->
	<div class="jv-nu jv-safe-bottom">
		<div class="jv-nu-card">
			<BrandMark :size="56" />

			<span v-if="notice.version" class="jv-nu-badge">Version {{ notice.version }}</span>

			<h1 class="jv-nu-title">A new {{ agentName }} update is available</h1>

			<p v-if="notice.message" class="jv-nu-msg">{{ notice.message }}</p>

			<p class="jv-nu-block">
				Chat with {{ agentName }} is paused for this workspace until it's updated. Please
				ask your administrator to update.
			</p>

			<button class="jv-nu-btn" :disabled="checking" @click="recheck">
				{{ checking ? "Checking…" : "I've updated — check again" }}
			</button>
		</div>
	</div>
</template>

<style scoped>
.jv-nu {
	position: fixed;
	inset: 0;
	z-index: 1000;
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 32px 24px;
	background: var(--card, #fff);
	overflow-y: auto;
}
.jv-nu-card {
	display: flex;
	flex-direction: column;
	align-items: center;
	text-align: center;
	max-width: 360px;
	width: 100%;
	gap: 12px;
}
.jv-nu-badge {
	margin-top: 4px;
	padding: 3px 10px;
	border-radius: 999px;
	font-size: 12px;
	font-weight: 600;
	color: var(--accent, #6e5cf6);
	background: rgba(110, 92, 246, 0.12);
}
.jv-nu-title {
	margin: 2px 0 0;
	font-size: 22px;
	font-weight: 600;
	letter-spacing: -0.3px;
	color: var(--ink9, #171717);
}
.jv-nu-msg {
	margin: 0;
	font-size: 15px;
	line-height: 1.55;
	color: var(--ink6, #6b7280);
	white-space: pre-line;
}
.jv-nu-block {
	margin: 0;
	font-size: 15px;
	line-height: 1.55;
	font-weight: 500;
	color: var(--ink8, #2f2f37);
}
.jv-nu-btn {
	margin-top: 8px;
	width: 100%;
	max-width: 280px;
	height: 48px;
	border: 0;
	border-radius: 12px;
	background: var(--accent-solid, #6e5cf6);
	color: #fff;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-nu-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
</style>
