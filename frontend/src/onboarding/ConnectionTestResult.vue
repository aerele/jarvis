<template>
	<div v-if="rows.length || banner" class="flex flex-col gap-3">
		<div v-if="rows.length" class="overflow-hidden rounded-lg border border-outline-gray-1">
			<div
				class="border-b border-outline-gray-1 bg-surface-gray-1 px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-gray-5"
			>
				Connection test
			</div>
			<div
				v-for="(row, i) in rows"
				:key="i"
				class="flex items-start gap-2.5 px-4 py-3 text-p-sm"
				:class="i > 0 ? 'border-t border-outline-gray-1' : ''"
			>
				<span class="grid size-5 flex-none place-items-center" aria-hidden="true">
					<JvSpinner v-if="row.state === 'checking'" :size="20" />
					<FeatherIcon
						v-else-if="row.state === 'pass'"
						name="check-circle"
						class="h-4 w-4 text-ink-green-3"
					/>
					<FeatherIcon v-else name="x-circle" class="h-4 w-4 text-ink-red-3" />
				</span>
				<span class="flex min-w-0 flex-col gap-0.5">
					<span class="font-medium text-ink-gray-9"
						><span class="sr-only">{{ stateWord(row.state) }}: </span
						>{{ row.label }}</span
					>
					<span class="text-p-xs text-ink-gray-5">{{ row.detail }}</span>
				</span>
			</div>
		</div>
		<Banner
			v-if="banner"
			:type="banner.type"
			:title="banner.title"
			:message="banner.text"
			:role="banner.type === 'success' ? 'status' : 'alert'"
		/>
	</div>
</template>

<script setup>
// What the customer sees after pressing Test connection: one row per half of the test and one
// banner. The rows and banner come from privateNetwork.describeServerTest (copy from
// TestMessages.dc.html); this component only draws them.
import { FeatherIcon } from "frappe-ui";

import Banner from "@/components/Banner.vue";
import JvSpinner from "@/components/JvSpinner.vue";

function stateWord(state) {
	return state === "pass" ? "Passed" : state === "checking" ? "Checking" : "Failed";
}

defineProps({
	rows: { type: Array, default: () => [] },
	banner: { type: Object, default: null },
});
</script>
