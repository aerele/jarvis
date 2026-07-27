<!--
  Reusable inline banner (design.md §3.7 "Banners & notices"). Sits in a fixed
  slot on a screen (a form step, a blocked action) instead of loose colored
  text. Icon tile + title/message body + an optional #action slot for a
  Retry-style button. Wired into onboarding's error/notice surfaces (9 spots
  in OnboardingView.vue) and ChatView's billing/paused banners — the
  type/title/message props and the default/#action slots are a stable API
  that every call site depends on; only the internal styling below changed.
-->
<template>
	<div class="flex items-start gap-2.5 rounded-md p-2.5" :class="variant.fill">
		<span
			class="grid size-6.5 shrink-0 place-items-center rounded-lg border bg-surface-white"
			:class="variant.border"
			aria-hidden="true"
		>
			<FeatherIcon :name="variant.icon" class="size-3.5" :class="variant.ink" />
		</span>
		<div class="min-w-0 flex-1">
			<template v-if="title">
				<div class="text-sm font-semibold" :class="variant.ink">{{ title }}</div>
				<div v-if="message" class="mt-0.5 text-p-xs text-ink-gray-7">{{ message }}</div>
			</template>
			<!-- No title: the message reads as the primary line so a message-only
				 banner (the common case here) doesn't look like a blank card with a
				 muted second line. -->
			<div v-else class="text-sm font-semibold" :class="variant.ink">{{ message }}</div>
			<!-- Optional extra body (hint line, a details expander): renders nothing
				 when no default slot is passed, so existing message-only callers are
				 unaffected. -->
			<slot />
		</div>
		<div v-if="$slots.action" class="mt-0.5 flex shrink-0 items-center gap-2">
			<slot name="action" />
		</div>
	</div>
</template>

<script setup>
import { computed } from "vue";
import { FeatherIcon } from "frappe-ui";

const props = defineProps({
	type: { type: String, default: "error" }, // error | warning | info | success
	title: { type: String, default: "" },
	message: { type: String, default: "" },
});

// Typed fill + ink, straight off design.md §3.7's banner recipe and the same
// red/amber/green/blue ramps frappe-ui's own Badge uses (bg-surface-{hue}-2 +
// text-ink-{hue}-3, confirmed against Badge.vue's subtle theme map). Error
// uses ink-red-3 rather than Badge's ink-red-4 — design.md §2.1 documents
// red-3 specifically as "error-banner ink", red-4 as badge/general error text.
//
// Info is the real blue ramp, not --cta: the old jv-* version painted "info"
// with --cta/--cta-bg to dodge a since-fixed gap where --info was never
// defined, but PR #294 repointed --cta off indigo to near-black/near-white,
// so an "info" banner was silently rendering as a near-black/near-white card.
// Using text-ink-blue-3 / bg-surface-blue-2 here is the fix, not a stylistic
// swap.
const VARIANTS = {
	error: {
		fill: "bg-surface-red-2",
		border: "border-outline-red-2",
		ink: "text-ink-red-3",
		icon: "alert-circle",
	},
	warning: {
		fill: "bg-surface-amber-2",
		border: "border-outline-amber-2",
		ink: "text-ink-amber-3",
		icon: "alert-triangle",
	},
	success: {
		fill: "bg-surface-green-2",
		border: "border-outline-green-2",
		ink: "text-ink-green-3",
		icon: "check-circle",
	},
	info: {
		fill: "bg-surface-blue-2",
		border: "border-outline-blue-1",
		ink: "text-ink-blue-3",
		icon: "info",
	},
};
const variant = computed(() => VARIANTS[props.type] || VARIANTS.error);
</script>
