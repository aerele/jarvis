<template>
	<span
		class="inline-flex h-5 shrink-0 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-2 text-xs text-ink-violet-1"
	>
		{{ label }}
	</span>
</template>

<script setup>
// The brand's shadow / preview state is violet, and frappe-ui's Badge ships no
// violet theme (see ActivationPanel.vue) - so this small pill is the single home
// for the hand-rolled violet chip rather than copy-pasting the classes at each
// call site.
defineProps({
	label: { type: String, default: "Shadow" },
});
</script>
