<template>
	<div class="mt-4">
		<!-- header: a live "Running for mm:ss" + status pill while the run is in
		     flight; a plain disclosure once it is over, so a finished run can still
		     be inspected without the timeline stealing the page. -->
		<div v-if="isRunning" class="flex items-center gap-2">
			<span class="text-base font-medium text-ink-gray-8">Running for {{ elapsed }}</span>
			<Badge variant="subtle" theme="blue" :label="status" />
			<span class="flex-1" />
			<span class="text-sm text-ink-gray-5">{{ countLabel }}</span>
		</div>
		<button
			v-else-if="steps.length"
			type="button"
			class="flex w-full items-center gap-2 text-left"
			:aria-expanded="open"
			@click="open = !open"
		>
			<FeatherIcon
				name="chevron-right"
				class="size-4 shrink-0 text-ink-gray-5 transition-all duration-300 ease-in-out"
				:class="{ 'rotate-90': open }"
			/>
			<span class="text-base font-medium text-ink-gray-8">Steps ({{ steps.length }})</span>
		</button>

		<!-- the timeline itself: the same row shape as the Activity tab (icon disc,
		     title line, muted detail, relative time on the right). -->
		<div v-if="expanded" class="mt-2 divide-y overflow-hidden rounded-lg border">
			<div
				v-for="(s, i) in steps"
				:key="s.name"
				class="flex items-start gap-3 px-3 py-2.5"
				:class="isCurrent(i) ? 'bg-surface-gray-1' : ''"
			>
				<div
					class="grid size-8 shrink-0 place-items-center rounded-full bg-surface-gray-2"
				>
					<FeatherIcon :name="stepIcon(s)" class="size-4" :class="stepColor(s)" />
				</div>
				<div class="min-w-0 flex-1">
					<div class="flex flex-wrap items-baseline gap-x-2">
						<span
							class="text-base font-medium"
							:class="s.status === 'error' ? 'text-ink-red-4' : 'text-ink-gray-8'"
						>
							{{ s.label }}
						</span>
						<Badge v-if="isCurrent(i)" variant="subtle" theme="blue" label="Now" />
					</div>
					<div v-if="s.detail" class="mt-0.5 truncate text-sm text-ink-gray-6">
						{{ s.detail }}
					</div>
				</div>
				<div class="flex shrink-0 items-baseline gap-2 text-sm text-ink-gray-5">
					<span v-if="durationLabel(s)">{{ durationLabel(s) }}</span>
					<Tooltip :text="exactDate(stepTime(s))">
						<span>{{ timeAgo(stepTime(s)) }}</span>
					</Tooltip>
				</div>
			</div>
		</div>

		<!-- before the delegate's first tool call there is exactly one thing to say,
		     and it is not "no steps": the bench dispatched the turn and is waiting. -->
		<div v-else-if="isRunning" class="mt-2 py-6 text-sm text-ink-gray-5">
			Dispatched to the agent, waiting for the first step.
		</div>
	</div>
</template>

<script setup>
// RunStepTimeline - the live per-run step feed (jarvis#1062, child of #1058).
// The bench records one Jarvis Agent Run Step per observable step of a run: the
// launch dispatch, every jarvis__* tool the delegate called back with, and the
// findings writeback. This renders them in the Activity tab's row language
// (icon disc + title + muted detail + relative time) rather than a bespoke
// widget, so the Runs pane and the Activity tab read as one product.
//
// While the run is in flight the list is always open and the newest step is
// marked "Now". Once it is terminal the whole thing collapses behind a
// "Steps (N)" disclosure, so a finished run stays inspectable without the
// timeline competing with its findings.
import { ref, computed } from "vue";
import { Badge, FeatherIcon, Tooltip } from "frappe-ui";
import { timeAgo, exactDate } from "@/utils/datetime";

const props = defineProps({
	// rows from agents_api.list_run_steps: {name, seq, kind, tool, label,
	// detail, status, duration_ms, occurred_at, creation}
	steps: { type: Array, default: () => [] },
	// the run's status - drives running vs collapsed-disclosure presentation
	status: { type: String, default: "" },
	// the parent's ticking "mm:ss" label (one timer for the whole panel)
	elapsed: { type: String, default: "" },
});

// per-kind Feather icon, from the set the Agents pages already use. An error
// step overrides both icon and colour: a failed step must read as failed at a
// glance, exactly as run_failed does on the Activity tab.
const KIND_ICON = {
	dispatched: "play",
	tool: "search",
	writeback: "check-circle",
	note: "activity",
};
const KIND_COLOR = {
	writeback: "text-ink-green-3",
};

const open = ref(false);

const isRunning = computed(() => props.status === "running");
const expanded = computed(() => props.steps.length > 0 && (isRunning.value || open.value));
const countLabel = computed(() =>
	props.steps.length === 1 ? "1 step" : `${props.steps.length} steps`
);

function isCurrent(index) {
	return isRunning.value && index === props.steps.length - 1;
}
function stepIcon(s) {
	if (s.status === "error") return "x-circle";
	return KIND_ICON[s.kind] || "activity";
}
function stepColor(s) {
	if (s.status === "error") return "text-ink-red-4";
	return KIND_COLOR[s.kind] || "text-ink-gray-5";
}
function stepTime(s) {
	return s.occurred_at || s.creation;
}
// Sub-second steps are the common case, so ms below a second and one decimal
// above it - never a bare "0s" that reads as "did not happen".
function durationLabel(s) {
	const ms = Number(s.duration_ms);
	if (!ms || isNaN(ms) || ms < 0) return "";
	return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}
</script>
