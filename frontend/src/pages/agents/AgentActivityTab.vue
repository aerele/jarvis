<template>
	<div class="flex h-full min-h-0 flex-col">
		<!-- toolbar: search only (the feed has no category/sort) -->
		<div class="flex items-center gap-2 px-5 pt-4">
			<FormControl
				type="text"
				class="w-60"
				placeholder="Search activity"
				:modelValue="search"
				@update:modelValue="(v) => (search = v)"
			>
				<template #prefix>
					<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
				</template>
			</FormControl>
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto px-5 pb-8 pt-2">
			<div v-if="loading && !rows.length" class="py-10 text-center text-sm text-ink-gray-5">
				Loading activity…
			</div>
			<div
				v-else-if="error && !rows.length"
				class="py-10 text-center text-sm text-ink-red-4"
			>
				{{ error }}
			</div>
			<div
				v-else-if="!rows.length"
				class="flex flex-col items-center gap-1 py-16 text-center"
			>
				<FeatherIcon name="activity" class="size-7.5 text-ink-gray-5" />
				<span class="mt-2 text-lg font-medium text-ink-gray-8">No activity yet</span>
				<span class="text-p-base text-ink-gray-6">
					Installs, schedule changes and runs will show up here.
				</span>
			</div>

			<div v-else class="divide-y">
				<div v-for="r in rows" :key="r.name" class="flex items-start gap-3 py-3">
					<div
						class="grid size-8 shrink-0 place-items-center rounded-full bg-surface-gray-2"
					>
						<FeatherIcon
							:name="actionIcon(r.action)"
							class="size-4"
							:class="actionColor(r.action)"
						/>
					</div>
					<div class="min-w-0 flex-1">
						<div class="flex flex-wrap items-baseline gap-x-2">
							<span class="text-base font-medium text-ink-gray-8">
								{{ r.agent_title || r.agent }}
							</span>
							<span class="text-sm text-ink-gray-5">{{
								actionLabel(r.action)
							}}</span>
						</div>
						<div v-if="r.detail" class="mt-0.5 truncate text-sm text-ink-gray-6">
							{{ r.detail }}
						</div>
					</div>
					<Tooltip :text="exactDate(r.creation)">
						<span class="shrink-0 text-sm text-ink-gray-5">{{
							timeAgo(r.creation)
						}}</span>
					</Tooltip>
				</div>
			</div>
		</div>

		<ListFooter
			v-if="rows.length"
			class="shrink-0 border-t px-5 py-2"
			:modelValue="pageLength"
			:options="{ rowCount: rows.length, totalCount: total }"
			@update:modelValue="(v) => (pageLength = v)"
			@loadMore="loadMore"
		/>
	</div>
</template>

<script setup>
// Agent activity feed - the Agents page's 4th tab. Self-contained: owns its
// useListPage bound to list_agent_activity_page (owner-scoped, newest first),
// its own debounced search and its own ListFooter, so AgentsList only mounts
// it when #activity is active (lazy first fetch via useListPage's onMounted).
// Rows are Link-free snapshots: {agent_title, action, detail, creation, run}.
import { FeatherIcon, FormControl, ListFooter, Tooltip } from "frappe-ui";
import { useListPage } from "@/composables/useListPage";
import { timeAgo, exactDate } from "@/utils/datetime";
import { listAgentActivityPage } from "@/api/agents";

// per-action Feather icon + label + ink color (lifecycle verbs from
// agents_api.list_agent_activity_page)
const ACTIONS = {
	installed: { icon: "download", label: "Installed" },
	uninstalled: { icon: "trash", label: "Uninstalled" },
	enabled: { icon: "check", label: "Enabled", color: "text-ink-green-3" },
	disabled: { icon: "slash", label: "Disabled" },
	schedule_changed: { icon: "clock", label: "Schedule changed" },
	config_changed: { icon: "sliders", label: "Configuration changed" },
	run_started: { icon: "play", label: "Run started" },
	run_completed: { icon: "check-circle", label: "Run completed", color: "text-ink-green-3" },
	run_partial: {
		icon: "alert-triangle",
		label: "Run completed with issues",
		color: "text-ink-amber-3",
	},
	run_failed: { icon: "x-circle", label: "Run failed", color: "text-ink-red-4" },
};
function actionIcon(a) {
	return (ACTIONS[a] || {}).icon || "activity";
}
function actionLabel(a) {
	return (
		(ACTIONS[a] || {}).label ||
		String(a || "")
			.split("_")
			.join(" ")
	);
}
function actionColor(a) {
	return (ACTIONS[a] || {}).color || "text-ink-gray-5";
}

// useListPage adapter: {search, start, page_length} → listAgentActivityPage
// (its filters/sort_field don't apply - the feed is fixed newest-first)
const { rows, total, loading, error, search, pageLength, loadMore } = useListPage({
	fetchFn: (p) =>
		listAgentActivityPage({ search: p.search, start: p.start, page_length: p.page_length }),
	storageKey: "agents-activity",
});
</script>
