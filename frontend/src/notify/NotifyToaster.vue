<template>
	<div
		class="pointer-events-none fixed bottom-5 right-5 z-[100] flex w-[340px] max-w-[calc(100vw-40px)] flex-col gap-2"
		aria-live="polite"
	>
		<TransitionGroup name="jn-toast">
			<div
				v-for="t in toasts"
				:key="t.id"
				class="pointer-events-auto flex cursor-pointer items-center gap-3 rounded-xl border border-outline-gray-2 bg-surface-white p-3 shadow-xl"
				role="status"
				@click="open(t)"
			>
				<JarvisMark :size="32" :radius="8" />
				<div class="min-w-0 flex-1">
					<div class="truncate text-sm font-semibold text-ink-gray-8">{{ t.title }}</div>
					<div v-if="t.body" class="truncate text-xs text-ink-gray-5">{{ t.body }}</div>
				</div>
				<button
					class="flex size-6 flex-none items-center justify-center rounded-md text-ink-gray-4 hover:bg-surface-gray-2 hover:text-ink-gray-8"
					title="Dismiss"
					aria-label="Dismiss"
					@click.stop="dismissToast(t.id)"
				>
					<svg
						width="13"
						height="13"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>
		</TransitionGroup>
	</div>
</template>

<script setup>
// App-shell toaster for the global notifier (NOTIFY-APPROVALS Part 1): small
// bottom-right stack (max 3, 6s auto-dismiss — enforced in globalNotifier.js),
// title + body + click-through to the event's page. Semantic tokens only, so
// it tracks light/dark like the rest of the shell. Distinct from ChatView's
// jv-notes (status feedback) and jv-toast (proactive card inside the chat pane).
import JarvisMark from "@/components/JarvisMark.vue";
import { useToasts, dismissToast } from "./globalNotifier";

const toasts = useToasts();

function open(t) {
	if (t.onClick) t.onClick();
	dismissToast(t.id);
}
</script>

<style scoped>
.jn-toast-enter-active,
.jn-toast-leave-active {
	transition: opacity 0.18s ease, transform 0.18s ease;
}
.jn-toast-enter-from,
.jn-toast-leave-to {
	opacity: 0;
	transform: translateY(8px);
}
</style>
