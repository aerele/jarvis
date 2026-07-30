<template>
	<!-- Selected-row check glyph, shared by the composer pills so the SVG is not
	     forked (was an inline template SVG in PersonaPill and an h() render in
	     ModelEffortPicker). Colour follows the surrounding text. -->
	<svg
		class="jv-check"
		width="15"
		height="15"
		viewBox="0 0 24 24"
		fill="none"
		stroke="currentColor"
		stroke-width="2.2"
		stroke-linecap="round"
		stroke-linejoin="round"
		aria-hidden="true"
	>
		<path d="M20 6 9 17l-5-5" />
	</svg>
</template>

<style scoped>
.jv-check {
	flex: none;
	color: var(--text);
}
</style>
