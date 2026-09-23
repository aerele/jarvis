<template>
	<!-- A stated fact plus the single affordance that acts on it, side by side.
	     The billing page has three of these (cancelling, scheduled switch,
	     autopay off) and they must read as one pattern, not three. -->
	<div
		class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-outline-gray-1 p-4"
	>
		<span class="text-p-sm text-ink-gray-7">{{ message }}</span>
		<Button
			v-if="actionLabel"
			:variant="solid ? 'solid' : 'subtle'"
			:theme="theme || undefined"
			:label="actionLabel"
			:loading="loading"
			@click="emit('action')"
		/>
	</div>
</template>

<script setup>
import { Button } from "frappe-ui";

defineProps({
	message: { type: String, required: true },
	actionLabel: { type: String, default: "" },
	/** Reserved for the one notice whose action is the page's primary move. */
	solid: { type: Boolean, default: false },
	loading: { type: Boolean, default: false },
	/** Optional Button theme (e.g. "red"). Pair only with solid=false (the
	 *  default) to stay red-SUBTLE - combining with solid would render red-SOLID
	 *  and break the "confirm dialog owns the deliberate red step" convention. */
	theme: { type: String, default: "" },
});
const emit = defineEmits(["action"]);
</script>
