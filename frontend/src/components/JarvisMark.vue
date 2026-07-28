<template>
	<!-- The brand mark. Single source of truth for the brand glyph so the
	     onboarding wizard, the onboarding gate poster, the chat avatars (and any
	     future setup surface) can't drift apart on a brand refresh. When the
	     tenant has uploaded a whitelabel logo we render it in place of the
	     default gradient spark. -->
	<img
		v-if="brandLogoUrl"
		class="jv-mark jv-mark-img"
		:src="brandLogoUrl"
		:style="{ width: `${size}px`, height: `${size}px`, borderRadius: `${radius}px` }"
		alt=""
	/>
	<span
		v-else
		class="jv-mark"
		:style="{ width: `${size}px`, height: `${size}px`, borderRadius: `${radius}px` }"
	>
		<svg
			:width="Math.round(size * 0.55)"
			:height="Math.round(size * 0.55)"
			viewBox="0 0 24 24"
			fill="#fff"
		>
			<path :d="BRAND_STAR_PATH" />
		</svg>
	</span>
</template>

<script setup>
import { brandLogoUrl } from "@/branding";
import { BRAND_STAR_PATH } from "@/lib/brand";

defineProps({
	size: { type: Number, default: 56 },
	radius: { type: Number, default: 14 },
});
</script>

<style scoped>
.jv-mark {
	display: grid;
	place-items: center;
	flex-shrink: 0;
	/* --brand-grad is defined on :root in main.css (theme-invariant). The literal
	   fallback keeps the mark correct if this component is ever rendered outside
	   the app's stylesheet (e.g. an isolated story or a test harness). */
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
}
/* A tenant logo fills the same square footprint; cover keeps it edge-to-edge
   at any aspect ratio without distortion. */
.jv-mark-img {
	object-fit: cover;
	display: block;
	flex-shrink: 0;
	background: var(--surface-2, #f0f0f4);
}
</style>
