<template>
	<SettingsPane title="Activity" description="Recent tool calls in this chat.">
		<h3 class="text-base font-semibold text-ink-gray-9">Recent tool runs</h3>

		<div
			v-if="!recentActivity.length"
			class="flex flex-col items-center gap-2 py-12 text-center"
		>
			<FeatherIcon name="activity" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">No tool activity in this chat yet.</span>
		</div>

		<div v-else class="mt-2">
			<div
				v-for="(a, i) in recentActivity"
				:key="i"
				class="border-t py-2.5 first:border-t-0 first:pt-0"
			>
				<div class="flex items-center gap-2 text-p-sm text-ink-gray-7">
					<FeatherIcon name="tool" class="size-4 text-ink-gray-5" />
					<span>{{ a.tools }} tool{{ a.tools === 1 ? "" : "s" }}</span>
					<span class="ml-auto tabular-nums text-ink-gray-5"
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
import { computed } from "vue";
import { FeatherIcon } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import SettingsPane from "@/components/settings/SettingsPane.vue";

const shell = useShellStore();
const recentActivity = computed(() => shell.chatContext?.sessionStats?.recentActivity || []);
</script>
