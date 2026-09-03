<template>
	<DocSection
		v-if="details.length"
		label="Technical details"
		:opened="false"
		class="border-t-0 pt-0"
	>
		<dl class="space-y-1 text-xs">
			<div v-for="(d, i) in details" :key="i" class="flex gap-2">
				<dt class="w-32 shrink-0 text-ink-gray-5">{{ d.label }}</dt>
				<dd class="min-w-0 flex-1 break-all font-mono text-ink-gray-7">{{ d.value }}</dd>
			</div>
		</dl>
	</DocSection>
</template>

<script setup>
// TechnicalDetails - jarvis#1062 P0-2/P1-3 (production-readiness audit):
// findings and coverage banners are bundle-generated prose that carries
// machine tokens (rule codes, boolean eval flags, "N class(es)
// not_evaluable" counts, DocType.field references) - @/lib/findingText's
// extractTechnicalDetails() pulls these out of the primary sentence; this
// renders the result as a collapsed, labelled, monospace list. Shared by
// FindingsPanel.vue's expanded finding AND its coverage/warning banners, so
// there is exactly one place this ever renders differently.
import DocSection from "@/components/doc/DocSection.vue";

defineProps({
	// [{label, value}] from extractTechnicalDetails - renders nothing (not
	// even an empty DocSection header) when empty.
	details: { type: Array, default: () => [] },
});
</script>
