<template>
	<!-- Collapse-to-expand reply wrapper, modelled on Helpdesk's TicketTextEditor:
	     a one-line "simple input" bar that swaps to the full SupportComposer on
	     click. The editor is v-if-mounted (NOT hidden) so TipTap/ProseMirror isn't
	     constructed while collapsed — that's both the perf win and the literal
	     "simple input that expands". `expanded` is parent-driven (v-model:expanded)
	     so the host collapses it after a successful send and the details-panel
	     Reply button can open it, without this component knowing either flow. -->
	<div class="jv-suprb">
		<SupportComposer
			v-if="expanded"
			:model-value="modelValue"
			:pending="pending"
			:can-submit="canSubmit"
			:loading="loading"
			:submit-label="submitLabel"
			:placeholder="placeholder"
			autofocus
			@update:model-value="(v) => emit('update:modelValue', v)"
			@submit="emit('submit')"
			@files-added="(f) => emit('files-added', f)"
			@remove-attachment="(i) => emit('remove-attachment', i)"
		/>
		<!-- Real <button> (not Helpdesk's bare <div @click>) so Enter/Space open it
		     for keyboard users, same a11y rationale as SupportComposer's Attach. -->
		<button
			v-else
			type="button"
			data-test="reply-bar"
			class="jv-suprb-bar"
			@click="emit('update:expanded', true)"
		>
			<span class="jv-suprb-avatar" aria-hidden="true">
				<FeatherIcon name="edit-2" class="size-4" />
			</span>
			<span class="jv-suprb-placeholder">{{ placeholder || "Reply…" }}</span>
		</button>
		<!-- Disclaimer (the reopen-on-reply note) lives on the WRAPPER so it shows in
		     BOTH states — it's decision-relevant before the user expands. The inner
		     composer is therefore NOT given a disclaimer (no double render). -->
		<div v-if="disclaimer" class="jv-suprb-disclaimer">{{ disclaimer }}</div>
	</div>
</template>

<script setup>
import { FeatherIcon } from "frappe-ui";
import SupportComposer from "@/components/support/SupportComposer.vue";

defineProps({
	// Forwarded to SupportComposer when expanded (the host owns all of these).
	modelValue: { type: String, default: "" },
	pending: { type: Array, default: () => [] },
	canSubmit: { type: Boolean, default: false },
	loading: { type: Boolean, default: false },
	submitLabel: { type: String, default: "Send" },
	placeholder: { type: String, default: "" },
	// Fine print shown in both collapsed and expanded states.
	disclaimer: { type: String, default: "" },
	// v-model:expanded — parent-driven (collapse-after-send / panel Reply / switch).
	expanded: { type: Boolean, default: false },
});

const emit = defineEmits([
	"update:modelValue",
	"submit",
	"files-added",
	"remove-attachment",
	"update:expanded",
]);
</script>

<style scoped>
/* The collapsed bar echoes the expanded composer card's shape (border/radius) so
   the swap reads as the same control opening up. Rendered inside the thread's
   chat-surface (jv-root), so it uses the jv palette vars. */
.jv-suprb-bar {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	padding: 10px 14px;
	border: 1px solid var(--border);
	border-radius: 13px;
	background: var(--surface);
	color: var(--text-3);
	font-size: 14px;
	text-align: left;
	cursor: text;
	transition: border-color 0.12s, background 0.12s;
}
.jv-suprb-bar:hover {
	border-color: var(--text-2);
}
.jv-suprb-bar:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
.jv-suprb-avatar {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 26px;
	height: 26px;
	border-radius: 999px;
	background: var(--surface-2);
	color: var(--text-2);
	flex: 0 0 auto;
}
.jv-suprb-placeholder {
	color: var(--text-3);
}
.jv-suprb-disclaimer {
	text-align: center;
	font-size: 10.5px;
	color: var(--text-3);
	margin-top: 8px;
}
</style>
