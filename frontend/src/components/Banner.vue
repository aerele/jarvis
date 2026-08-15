<!--
  Reusable inline banner (design.md §3.7 "Banners & notices"). Sits in a fixed
  slot on a screen (a form step, a blocked action) instead of loose colored
  text. Icon + title/message body + an optional #action slot for a
  Retry-style button. Wired into onboarding's error/notice surfaces (9 spots
  in OnboardingView.vue) and ChatView's billing/paused banners — the
  type/title/message props and the default/#action slots are a stable API
  that every call site depends on; only the internal styling below changed.

  jarvis#725: the icon used to sit in a bordered size-6.5 (26px) chip next to
  `items-start`. A single text-sm line is only ~15px tall, so the chip's own
  centered icon landed well below the text's center - visibly top-heavy on
  the common single-line case. Two fixes were possible: switch to
  `items-center` (design.md's literal words), or shrink the icon box to the
  text's own line-height so `items-start` already centers it. items-center
  was rejected because it centers the icon against the WHOLE flex item - for
  a title+message banner (two lines) or a long message that wraps, that
  drifts the icon toward the block's middle instead of staying on the first
  line (verified in a throwaway visual harness, not shipped: items-center
  visibly slides the icon down to straddle both lines once a second line is
  added). Bare `size-4` (16px, design.md's own icon-size spec, no chip) sits
  so close to a text-sm line (~15px) that `items-start` alone reads as
  centered, and - because the icon no longer grows with the content - it
  stays pinned to line one no matter how many lines follow. This also
  matches the "Test failed" reference the issue called out as already
  correct (LlmPoolEditor's .jv-status: a bare icon, no chip).
-->
<template>
	<div class="flex items-start gap-2.5 rounded-md p-2.5" :class="variant.fill">
		<FeatherIcon
			:name="variant.icon"
			class="size-4 shrink-0"
			:class="variant.ink"
			aria-hidden="true"
		/>
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
		ink: "text-ink-red-3",
		icon: "alert-circle",
	},
	warning: {
		fill: "bg-surface-amber-2",
		ink: "text-ink-amber-3",
		icon: "alert-triangle",
	},
	success: {
		fill: "bg-surface-green-2",
		ink: "text-ink-green-3",
		icon: "check-circle",
	},
	info: {
		fill: "bg-surface-blue-2",
		ink: "text-ink-blue-3",
		icon: "info",
	},
};
const variant = computed(() => VARIANTS[props.type] || VARIANTS.error);
</script>
