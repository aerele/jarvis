<template>
	<router-link
		:to="'/shared/' + encodeURIComponent(conv.name)"
		class="mx-2 my-0.5 flex min-w-0 items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-gray-8 hover:bg-surface-gray-2 focus-visible:ring-2"
		active-class="bg-surface-selected"
		><span class="min-w-0 flex-1"
			><span class="block truncate">{{ conv.title || __("Shared chat") }}</span
			><span class="block truncate text-xs text-ink-gray-5">{{
				conv.owner_name
			}}</span></span
		><span
			v-if="conv.unseen"
			class="size-1.5 shrink-0 rounded-full bg-blue-500"
			role="img"
			:aria-label="__('Unseen shared chat')"
	/></router-link>
</template>
<script setup>
import { __ } from "@/lib/i18n";
defineProps({ conv: { type: Object, required: true } });
</script>
