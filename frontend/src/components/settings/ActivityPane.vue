<template>
	<SettingsPane title="Activity" description="Recent tool calls in this chat.">
		<h3 class="text-base font-semibold text-ink-gray-9">Recent tool runs</h3>

		<div v-if="loading" class="mt-2 text-p-sm text-ink-gray-6">Loading…</div>

		<div v-else-if="!runs.length" class="flex flex-col items-center gap-2 py-12 text-center">
			<FeatherIcon name="activity" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">No tool activity in this chat yet.</span>
		</div>

		<div v-else class="mt-2">
			<div
				v-for="(a, i) in runs"
				:key="i"
				class="border-t py-2.5 first:border-t-0 first:pt-0"
			>
				<div class="flex items-center gap-2 text-p-sm text-ink-gray-7">
					<FeatherIcon name="tool" class="size-4 text-ink-gray-5" />
					<span>{{ a.tools }} tool{{ a.tools === 1 ? "" : "s" }}</span>
					<span v-if="a.ms" class="ml-auto tabular-nums text-ink-gray-5"
						>{{ (a.ms / 1000).toFixed(1) }}s</span
					>
				</div>
				<div
					v-if="a.names.length"
					class="mt-1 break-words font-mono text-xs text-ink-gray-5"
				>
					{{ a.names.join(", ") }}
				</div>
			</div>
		</div>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted, watch } from "vue";
import { FeatherIcon } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import * as api from "@/api";

const shell = useShellStore();

// Persisted tool rows, fetched per chat. This pane used to read a per-run map
// the chat view stamps on the live run:end event, which exists only for the
// lifetime of that mount, so a reload, a route change, or simply reopening an
// older chat showed "no tool activity" over a transcript full of tool cards
// (#551). An audit surface has to read the records, not the event stream.
const runs = ref([]);
const loading = ref(true);
// Reload when the chat changes AND when it gains messages, so a turn that
// finishes while this pane is open lands here too. A STRING key on purpose:
// ChatView republishes chatContext as a fresh object on almost every state
// change, so an array/object key would compare unequal every time and refetch
// on each republish.
const chatKey = computed(
	() =>
		`${shell.chatContext?.conversationId || ""}:${
			shell.chatContext?.sessionStats?.msgCount || 0
		}`
);

async function loadActivity() {
	const conversation = shell.chatContext?.conversationId;
	if (!conversation) {
		runs.value = [];
		loading.value = false;
		return;
	}
	try {
		const r = await api.getToolActivity(conversation);
		runs.value = (r && r.runs) || [];
	} catch {
		runs.value = [];
	}
	loading.value = false;
}

onMounted(loadActivity);
watch(chatKey, loadActivity);
</script>
