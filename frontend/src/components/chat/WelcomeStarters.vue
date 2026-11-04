<template>
	<!-- The welcome screen's starter cards and the capability catalog, as two siblings
	     (no wrapper), so the normal layout and the holiday stage layout put them in the
	     same place. -->
	<div
		class="jv-welcome-grid"
		style="
			display: grid;
			grid-template-columns: 1fr 1fr;
			gap: 11px;
			text-align: left;
			margin-top: 26px;
		"
	>
		<button
			v-for="(s, i) in cards"
			:key="s.prompt"
			type="button"
			class="jv-suggest"
			@click="emit('select', s)"
			style="
				display: flex;
				gap: 11px;
				padding: 14px;
				background: var(--surface);
				border: 1px solid var(--border);
				border-radius: 10px;
				cursor: pointer;
				transition: border-color 0.12s, background 0.12s;
				text-align: left;
				font-family: inherit;
				color: inherit;
				width: 100%;
			"
		>
			<div
				:style="{
					width: '30px',
					height: '30px',
					flex: 'none',
					borderRadius: '8px',
					background: tint(i).bg,
					color: tint(i).fg,
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
				}"
				v-html="tint(i).icon"
			></div>
			<div style="min-width: 0">
				<div
					v-if="s.title"
					style="font-size: 13.5px; font-weight: 550; margin-bottom: 2px"
				>
					{{ s.title }}
				</div>
				<div style="font-size: 12.5px; color: var(--text-3); line-height: 1.4">
					{{ s.prompt }}
				</div>
			</div>
		</button>
	</div>
	<CapabilityCatalog @select="emit('select', $event)" />
</template>

<script setup>
// Starter cards, same shape as the original static ones (tinted icon tile + label over the
// prompt). They FILL the composer, never send: the parent does the filling on `select`.
import CapabilityCatalog from "@/components/CapabilityCatalog.vue";

defineProps({
	/** [{title?, prompt}], already synthesised or defaulted by the parent. */
	cards: { type: Array, required: true },
	/** (index) -> {bg, fg, icon}: the tint of the card at that position. */
	tint: { type: Function, required: true },
});
const emit = defineEmits(["select"]);
</script>

<style scoped>
.jv-suggest:hover {
	border-color: var(--border-2);
	background: var(--surface-1);
}
.jv-suggest:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* Phone width: two 14px-padded cards side by side are unreadable, so they stack. The grid
   is set inline, so this has to win with !important. */
@media (max-width: 640px) {
	.jv-welcome-grid {
		grid-template-columns: 1fr !important;
	}
}
</style>
