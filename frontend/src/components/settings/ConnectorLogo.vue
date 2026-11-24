<!-- Small brand mark for a connector's preset — shown in ConnectorRow and in
     AddConnectorDialog next to the App field. Same idiom as ProviderLogo.vue
     (inline SVG, self-contained, CSP-safe): a preset with no mark in
     connectorLogos.js (Custom URL, or any legacy/unknown preset) falls back
     to a generic FeatherIcon rather than breaking. GitHub's mark uses
     `currentColor` and has no color set here — the caller supplies a
     text-ink-* class the same way ConnectorRow already did for its old
     FeatherIcon; the other marks carry their own brand fill. -->
<template>
	<span
		class="jv-clogo"
		:title="label"
		:style="{ width: size + 'px', height: size + 'px' }"
		role="img"
		:aria-label="label + ' logo'"
	>
		<svg
			v-if="mark"
			:viewBox="mark.viewBox"
			width="100%"
			height="100%"
			:fill="mark.fill"
			aria-hidden="true"
		>
			<path :d="mark.path" />
		</svg>
		<FeatherIcon v-else :name="fallbackIcon" class="jv-clogo-fallback" />
	</span>
</template>

<script setup>
import { computed } from "vue";
import { FeatherIcon } from "frappe-ui";
import { CONNECTOR_LOGOS, CONNECTOR_LOGO_FALLBACK_ICON } from "./connectorLogos.js";

const props = defineProps({
	preset: { type: String, default: "" },
	size: { type: Number, default: 18 },
});

const mark = computed(() => CONNECTOR_LOGOS[props.preset] || null);
const fallbackIcon = CONNECTOR_LOGO_FALLBACK_ICON;
const label = computed(() => props.preset || "Connector");
</script>

<style scoped>
.jv-clogo {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	flex: 0 0 auto;
	vertical-align: middle;
}
.jv-clogo svg {
	display: block;
}
.jv-clogo-fallback {
	width: 100%;
	height: 100%;
}
</style>
