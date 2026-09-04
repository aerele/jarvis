<template>
	<div class="flex items-center gap-3 rounded-lg border p-3">
		<ConnectorLogo :preset="row.preset" :size="20" class="shrink-0 text-ink-gray-5" />
		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-1.5">
				<span class="truncate text-sm font-medium text-ink-gray-9">{{ row.label }}</span>
				<Tooltip :text="statusTip">
					<Badge variant="subtle" size="sm" :theme="statusTheme" :label="statusLabel" />
				</Tooltip>
			</div>
			<div class="truncate text-xs text-ink-gray-5">{{ row.base_url }}</div>
		</div>

		<Switch
			:modelValue="!!row.enabled"
			:disabled="!canManage || toggling"
			@update:modelValue="(v) => emit('toggle', v)"
		/>
		<Button
			variant="ghost"
			icon="refresh-cw"
			:loading="testing"
			:tooltip="'Test connection'"
			@click="emit('test')"
		/>
		<template v-if="canManage">
			<Button variant="ghost" icon="edit-2" :tooltip="'Edit'" @click="emit('edit')" />
			<Button
				variant="ghost"
				theme="red"
				icon="trash-2"
				:tooltip="'Delete'"
				@click="emit('delete')"
			/>
		</template>
	</div>
</template>

<script setup>
// One connector row — shared by ConnectorsPane's "Shared" and "Mine" lists.
// Copies PersonalisationSettings' row idiom (Badge + Switch + ghost icon
// Buttons) and PromotionStatusChip's Badge+Tooltip status idiom, adapted to
// this row's own three-state status (MCP_CONNECTORS_PLAN.md UI/UX decision
// #3: Connected / Failed / Disabled only — no "Needs auth" tier in v1).
import { computed } from "vue";
import { Badge, Button, Switch, Tooltip } from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";
import { timeAgo } from "@/utils/datetime";

const props = defineProps({
	row: { type: Object, required: true },
	// Shared rows are read-only for a non-admin: Test stays available (the
	// backend gates it on read, not write) but the toggle/edit/delete actions
	// are hidden entirely rather than shown disabled.
	canManage: { type: Boolean, default: true },
	// Split so flipping the Switch never spins the Test button and vice versa
	// (each control's :loading/:disabled reads only its own action's flag) —
	// mirrors PersonalisationSettings' rowActing, which likewise only ever
	// disables its own Switch and never leaks into another control.
	testing: { type: Boolean, default: false },
	toggling: { type: Boolean, default: false },
});
const emit = defineEmits(["test", "edit", "delete", "toggle"]);

const statusTheme = computed(() => {
	if (!props.row.enabled) return "gray";
	if (props.row.last_test_status === "Passed") return "green";
	if (props.row.last_test_status === "Failed") return "red";
	return "gray";
});
const statusLabel = computed(() => {
	if (!props.row.enabled) return "Disabled";
	if (props.row.last_test_status === "Passed") return "Connected";
	if (props.row.last_test_status === "Failed") return "Failed";
	return "Not tested";
});
const statusTip = computed(() => {
	const when = props.row.last_test_at ? ` ${timeAgo(props.row.last_test_at)}` : "";
	if (!props.row.enabled) return "Turned off, won't be offered in chat.";
	if (props.row.last_test_status === "Passed") return `Last test passed${when}.`;
	if (props.row.last_test_status === "Failed") return `Last test failed${when}.`;
	return "Run a test to confirm it's reachable.";
});
</script>
