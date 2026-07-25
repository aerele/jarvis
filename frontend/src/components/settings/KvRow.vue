<template>
	<!-- The one read-only label/value row for the settings surface. Replaces four
	     near-identical clones (jv-set-row, jv-mon-kv, jv-acct-kv, jv-dsub-kv) —
	     design.md §5 anti-pattern 19. Pass a plain `value`, or use the slot when
	     the value is a Badge/Button rather than text. -->
	<div class="flex items-center justify-between gap-4 py-2">
		<span class="text-sm text-ink-gray-6">{{ label }}</span>
		<span class="text-base text-ink-gray-8">
			<slot>{{ value }}</slot>
		</span>
	</div>
</template>

<script setup>
defineProps({
	label: { type: String, required: true },
	value: { type: [String, Number], default: "" },
});
</script>
