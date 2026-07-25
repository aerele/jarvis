<template>
	<!-- App-wide confirmation modal. Mounted ONCE in AppShell; driven entirely by
       the shared useConfirm() state. Its own z-index is 200 so a Remove/Delete
       inside the settings modal is still confirmable. Applies paletteVars +
       jv-dark on its root (jv- vars aren't global). See composables/useConfirm.js
       for the promise-based API.

       Teleported to <body>: the settings dialog migrated to frappe-ui's Dialog
       (reka DialogPortal renders into <body> at z-index 60, see main.css
       .dialog-overlay). An overlay left inline in AppShell is trapped in the
       app-root stacking context and paints BEHIND that body-level portal however
       high its own z-index, so this overlay must live at <body> too. There it is
       a sibling of the settings portal and z-index 200 > 60 actually wins.
       Without it the confirm opens behind the settings dialog with its buttons
       unclickable (jarvis#435). -->
	<Teleport to="body">
		<transition name="jv-confirm-fade">
			<div
				v-if="state"
				class="jv-confirm-overlay jv-root"
				:class="{ 'jv-dark': dark }"
				:style="paletteVars"
				@click.self="settleConfirm(false)"
			>
				<div
					class="jv-cdialog"
					role="alertdialog"
					aria-modal="true"
					aria-labelledby="jv-confirm-title"
				>
					<div id="jv-confirm-title" class="jv-cdialog-title">{{ state.title }}</div>
					<div v-if="state.message" class="jv-cdialog-msg">{{ state.message }}</div>
					<div class="jv-cdialog-foot">
						<button class="jv-btn jv-btn--ghost" @click="settleConfirm(false)">
							{{ state.cancelLabel }}
						</button>
						<button
							class="jv-btn"
							:class="state.danger ? 'jv-btn--danger' : 'jv-btn--primary'"
							@click="settleConfirm(true)"
						>
							{{ state.confirmLabel }}
						</button>
					</div>
				</div>
			</div>
		</transition>
	</Teleport>
</template>

<script setup>
import { onMounted, onBeforeUnmount } from "vue";
import { confirmState as state, settleConfirm } from "@/composables/useConfirm";
import { useJarvisTheme } from "@/theme";

// The jv- palette (--surface, --text, --red, …) is NOT global :root — every jv-
// surface applies it to its own root via paletteVars + a jv-dark class (see
// SettingsDialog). This dialog is a shell sibling, so it must do the same or its
// var(--…) styling and dark backdrop resolve to nothing.
const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// Escape cancels. Capture phase + stopPropagation so a global Escape handler on
// an underlying view (e.g. ChatView closing settings) does NOT also fire when the
// user is only dismissing this dialog.
function onKey(e) {
	if (e.key === "Escape" && state.value) {
		e.preventDefault();
		e.stopPropagation();
		settleConfirm(false);
	}
}
onMounted(() => window.addEventListener("keydown", onKey, true));
onBeforeUnmount(() => window.removeEventListener("keydown", onKey, true));
</script>

<style scoped>
/* Copied from ChatView's former inline confirm dialog so the visual is identical;
   own overlay + transition names keep it decoupled from ChatView's jv-skills-overlay
   (still used by the skills modal). Fixed + high z-index to clear the settings modal. */
.jv-confirm-overlay {
	position: fixed;
	inset: 0;
	z-index: 200;
	background: rgba(15, 15, 22, 0.34);
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 24px;
}
/* jv-dark is applied to the overlay itself (not an ancestor), matching the way
   paletteVars is scoped to this root. */
.jv-confirm-overlay.jv-dark {
	background: rgba(0, 0, 0, 0.55);
}

.jv-cdialog {
	width: 400px;
	max-width: 100%;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	padding: 20px;
	box-shadow: 0 24px 70px rgba(20, 20, 30, 0.28);
	animation: jv-confirm-popin 0.16s ease;
}
.jv-cdialog-title {
	font-size: 16px;
	font-weight: 650;
	color: var(--text);
}
.jv-cdialog-msg {
	margin-top: 8px;
	font-size: 13.5px;
	line-height: 1.5;
	color: var(--text-2);
}
.jv-cdialog-foot {
	display: flex;
	justify-content: flex-end;
	gap: 10px;
	margin-top: 20px;
}

.jv-confirm-fade-enter-active,
.jv-confirm-fade-leave-active {
	transition: opacity 0.16s ease;
}
.jv-confirm-fade-enter-from,
.jv-confirm-fade-leave-to {
	opacity: 0;
}
@keyframes jv-confirm-popin {
	from {
		transform: scale(0.98);
		opacity: 0.5;
	}
	to {
		transform: scale(1);
		opacity: 1;
	}
}

/* Respect OS "reduce motion" — the inline dialog this replaced was covered by
   ChatView's reduced-motion block; keep that accessibility guarantee here. */
@media (prefers-reduced-motion: reduce) {
	.jv-cdialog {
		animation: none;
	}
	.jv-confirm-fade-enter-active,
	.jv-confirm-fade-leave-active {
		transition: none;
	}
}
</style>
