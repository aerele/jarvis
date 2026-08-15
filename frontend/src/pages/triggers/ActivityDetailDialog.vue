<template>
	<Dialog
		v-model="show"
		:options="{
			title: (row && (row.trigger_label || row.trigger)) || 'Trigger activity',
			size: 'xl',
		}"
	>
		<template #body-content>
			<template v-if="row">
				<!-- status + type badges -->
				<div class="flex flex-wrap items-center gap-2">
					<Badge
						variant="subtle"
						:theme="STATUS_THEME[row.status] || 'gray'"
						:label="row.status || '-'"
					/>
					<Badge
						v-if="row.action_type === 'Script'"
						variant="subtle"
						theme="blue"
						label="Script"
					/>
					<span
						v-else-if="row.action_type === 'LLM'"
						class="inline-flex h-5 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-1.5 text-xs text-ink-violet-1"
					>
						LLM
					</span>
					<Tooltip v-if="row.creation" :text="exactDate(row.creation)">
						<span class="text-sm text-ink-gray-5">{{ timeAgo(row.creation) }}</span>
					</Tooltip>
				</div>

				<!-- summary -->
				<div v-if="row.summary" class="mt-4 text-p-base text-ink-gray-8">
					{{ row.summary }}
				</div>

				<!-- full detail: mono pre-wrap block -->
				<pre
					v-if="row.detail"
					class="mt-4 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-surface-gray-2 p-3 font-mono text-sm text-ink-gray-8"
					>{{ row.detail }}</pre
				>

				<!-- read-only KV rows (design §4.1) -->
				<div class="mt-4 flex flex-col">
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">Trigger</span>
						<router-link
							v-if="row.trigger"
							:to="{ name: 'TriggerDetail', params: { id: row.trigger } }"
							class="text-base text-ink-blue-link hover:underline"
							@click="show = false"
						>
							{{ row.trigger_label || row.trigger }}
						</router-link>
						<span v-else class="text-base text-ink-gray-8">-</span>
					</div>
					<div class="h-px bg-surface-gray-2" />
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">Target</span>
						<a
							v-if="row.target_doctype && row.target_docname"
							:href="deskUrl"
							target="_blank"
							rel="noopener"
							class="flex min-w-0 items-center gap-1 text-base text-ink-gray-8 hover:underline"
						>
							<span class="truncate"
								>{{ row.target_doctype }} · {{ row.target_docname }}</span
							>
							<FeatherIcon
								name="external-link"
								class="size-3.5 shrink-0 text-ink-gray-5"
							/>
						</a>
						<span v-else class="text-base text-ink-gray-8">-</span>
					</div>
					<div class="h-px bg-surface-gray-2" />
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">Event</span>
						<span class="text-base text-ink-gray-8">{{
							eventLabel(row.doc_event)
						}}</span>
					</div>
					<div class="h-px bg-surface-gray-2" />
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">Duration</span>
						<span class="text-base text-ink-gray-8">{{ durationLabel }}</span>
					</div>
					<div class="h-px bg-surface-gray-2" />
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">User</span>
						<span class="truncate text-base text-ink-gray-8">{{
							row.event_user || "-"
						}}</span>
					</div>
					<div class="h-px bg-surface-gray-2" />
					<div class="flex items-center justify-between py-2">
						<span class="text-sm text-ink-gray-6">When</span>
						<span class="text-base text-ink-gray-8">{{
							exactDate(row.creation) || "-"
						}}</span>
					</div>
				</div>
			</template>
		</template>
	</Dialog>
</template>

<script setup>
// ActivityDetailDialog - full detail for one trigger-activity row (opened by
// ActivityTab's row click and TriggerDetail's Recent-activity rows).
// Everything renders from the list row itself - the backend has no per-row
// endpoint. `detail` is optional (list_activity_page's current SQL omits it);
// its mono block appears the moment the rows carry the field.
import { computed } from "vue";
import { Badge, Dialog, FeatherIcon, Tooltip } from "frappe-ui";
import { timeAgo, exactDate } from "@/utils/datetime";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	row: { type: Object, default: null },
	caps: { type: Object, default: () => ({}) }, // for doc_event labels
});

const emit = defineEmits(["update:modelValue"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const STATUS_THEME = { Success: "green", Failed: "red", Blocked: "orange", Skipped: "gray" };

function eventLabel(value) {
	const hit = (props.caps.events || []).find((e) => e.value === value);
	return (hit && hit.label) || value || "-";
}

const deskUrl = computed(() => {
	if (!props.row) return "";
	const dt = String(props.row.target_doctype || "")
		.toLowerCase()
		.replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(props.row.target_docname || "")}`;
});

const durationLabel = computed(() => {
	const ms = props.row && props.row.duration_ms;
	if (ms == null || ms === "") return "-";
	const n = Number(ms);
	if (Number.isNaN(n)) return "-";
	if (n < 1000) return `${Math.round(n)} ms`;
	return `${(n / 1000).toFixed(1)} s`;
});
</script>
