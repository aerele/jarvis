<template>
	<section class="min-w-0 p-4 sm:p-6 text-ink-gray-8">
		<h1 class="text-lg font-semibold">{{ __("Shared with me") }}</h1>
		<p class="mt-1 text-sm text-ink-gray-5">
			{{ __("Chats colleagues have shared with you. Read only.") }}
		</p>
		<p
			v-if="store.sharedConversationsLoading && !store.sharedConversations.length"
			class="py-6 text-sm"
			role="status"
		>
			{{ __("Loading shared chats…") }}
		</p>
		<div v-if="store.sharedConversationsError" class="py-6 text-sm" role="status">
			{{ __("Couldn't load shared chats.") }}
			<button
				type="button"
				class="underline"
				@click="store.loadSharedConversations({ refresh: true })"
			>
				{{ __("Try again") }}
			</button>
		</div>
		<p
			v-if="
				!store.sharedConversations.length &&
				!store.sharedConversationsLoading &&
				!store.sharedConversationsError
			"
			class="py-6 text-sm text-ink-gray-5"
		>
			{{ __("No chats are shared with you right now.") }}
		</p>
		<div v-if="store.sharedConversations.length" class="mt-5 max-w-3xl">
			<SharedConversationRow
				v-for="row in store.sharedConversations"
				:key="row.name"
				:conv="row"
			/>
		</div>
	</section>
</template>
<script setup>
import { onMounted } from "vue";
import { useShellStore } from "@/stores/shell";
import SharedConversationRow from "@/components/shell/SharedConversationRow.vue";
import { __ } from "@/lib/i18n";
const store = useShellStore();
onMounted(() => store.loadSharedConversations());
</script>
