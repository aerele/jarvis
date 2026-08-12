<!-- "Copy this chat into your new ticket?" prompt, shown when a user opens a
     support ticket from an active chat session. Same shell/overlay/teleport
     pattern as ConfirmDialog.vue (see its own comment for why each piece is
     there - the body-lock, the z-index, the jv- palette scoping) but three
     answers instead of two: Yes (this once), No (this once), and Don't ask
     again (persists, see useSupportCopyPrompt.js / Jarvis User Settings'
     support_context_copy_pref). Mounted once in AppShell, above the router
     view - ChatView is the only surface that TRIGGERS it, but it must not be
     the one that mounts it: an earlier build mounted it in ChatView and a
     route change away from Chat while a prompt was pending unmounted it
     mid-flight, permanently wedging promptSupportCopy()'s module-scope
     resolver for the rest of the session. Same reasoning as ConfirmDialog
     living in AppShell. -->
<template>
	<Teleport to="body">
		<transition name="jv-scp-fade">
			<div
				v-if="state"
				class="jv-scp-overlay jv-root"
				:class="{ 'jv-dark': dark }"
				:style="paletteVars"
				@click.self="settle('no')"
			>
				<div
					class="jv-scp-dialog"
					role="alertdialog"
					aria-modal="true"
					aria-labelledby="jv-scp-title"
				>
					<div id="jv-scp-title" class="jv-scp-title">Add this chat to your ticket?</div>
					<div class="jv-scp-msg">
						Copy your last few messages into the ticket so support has context without
						you retyping it.
					</div>
					<div v-if="state.preview" class="jv-scp-preview">{{ state.preview }}</div>
					<div class="jv-scp-foot">
						<button
							class="jv-btn jv-btn--ghost jv-scp-dontask"
							@click="settle('dontask')"
						>
							Don't ask again
						</button>
						<div class="jv-scp-foot-main">
							<button class="jv-btn jv-btn--ghost" @click="settle('no')">No</button>
							<button class="jv-btn jv-btn--primary" @click="settle('yes')">
								Yes, copy it
							</button>
						</div>
					</div>
				</div>
			</div>
		</transition>
	</Teleport>
</template>

<script setup>
import { onMounted, onBeforeUnmount } from "vue";
import {
	copyPromptState as state,
	settleSupportCopyPrompt,
} from "@/composables/useSupportCopyPrompt";
import { useJarvisTheme } from "@/theme";

const { effectiveDark: dark, paletteVars } = useJarvisTheme();

function settle(val) {
	settleSupportCopyPrompt(val);
}

// Escape = "no" (same as declining), not "dontask" — dismissing once shouldn't
// silently opt out of ever being asked again.
function onKey(e) {
	if (e.key === "Escape" && state.value) {
		e.preventDefault();
		e.stopPropagation();
		settle("no");
	}
}
onMounted(() => window.addEventListener("keydown", onKey, true));
onBeforeUnmount(() => window.removeEventListener("keydown", onKey, true));
</script>

<style scoped>
.jv-scp-overlay {
	position: fixed;
	inset: 0;
	z-index: 200;
	pointer-events: auto;
	background: rgba(15, 15, 22, 0.34);
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 24px;
}
.jv-scp-overlay.jv-dark {
	background: rgba(0, 0, 0, 0.55);
}
.jv-scp-dialog {
	width: 440px;
	max-width: 100%;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	padding: 20px;
	box-shadow: 0 24px 70px rgba(20, 20, 30, 0.28);
	animation: jv-scp-popin 0.16s ease;
}
.jv-scp-title {
	font-size: 16px;
	font-weight: 650;
	color: var(--text);
}
.jv-scp-msg {
	margin-top: 8px;
	font-size: 13.5px;
	line-height: 1.5;
	color: var(--text-2);
}
.jv-scp-preview {
	margin-top: 12px;
	padding: 10px 12px;
	max-height: 130px;
	overflow-y: auto;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
	font-size: 12px;
	line-height: 1.5;
	color: var(--text-2);
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
.jv-scp-foot {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 10px;
	margin-top: 20px;
}
.jv-scp-foot-main {
	display: flex;
	gap: 10px;
}
/* "Don't ask again" is a real choice but a quieter one visually than Yes/No -
   left-aligned and unobtrusive so it doesn't compete with the two answers a
   user picks nearly every time. */
.jv-scp-dontask {
	font-size: 12.5px;
	color: var(--text-3);
}
.jv-scp-fade-enter-active,
.jv-scp-fade-leave-active {
	transition: opacity 0.16s ease;
}
.jv-scp-fade-enter-from,
.jv-scp-fade-leave-to {
	opacity: 0;
}
@keyframes jv-scp-popin {
	from {
		transform: scale(0.98);
		opacity: 0.5;
	}
	to {
		transform: scale(1);
		opacity: 1;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-scp-dialog {
		animation: none;
	}
	.jv-scp-fade-enter-active,
	.jv-scp-fade-leave-active {
		transition: none;
	}
}
</style>
