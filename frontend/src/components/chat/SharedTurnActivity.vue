<template>
	<StepsBox
		v-if="view.mode === 'live' || view.head"
		:view="view"
		:open="open"
		@toggle="open = !open"
	>
		<template #details>
			<template v-for="item in items" :key="item.key">
				<p v-if="item.kind === 'step'" class="jv-tool-step">
					<svg
						width="13"
						height="13"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						aria-hidden="true"
					>
						<path d="m5 12 4 4L19 6" /></svg
					><span>{{ item.text }}</span>
				</p>
				<SharedToolActivity
					v-else
					:message="item.row"
					:loadDetail="loadDetail"
					:startOpen="tools.length === 1"
				/>
			</template>
		</template>
	</StepsBox>
</template>
<script setup>
import { computed, ref, inject, watch } from "vue";
import StepsBox from "./StepsBox.vue";
import SharedToolActivity from "./SharedToolActivity.vue";
import { activityItems, foldedHead, turnToolNames, liveBox } from "@/lib/liveTurn";
import { __ } from "@/lib/i18n";
const props = defineProps({
	message: { type: Object, required: true },
	tools: { type: Array, default: () => [] },
	turnState: { type: String, default: "" },
	loadDetail: { type: Function, default: undefined },
});
const open = ref(false),
	cache = inject("sharedToolDetails", null);
const list = (value) => {
	try {
		const data = typeof value === "string" ? JSON.parse(value) : value;
		return Array.isArray(data) ? data : [];
	} catch {
		return [];
	}
};
const items = computed(() =>
	activityItems(
		(list(props.message.steps).length
			? list(props.message.steps)
			: list(props.message.live_steps)
		).map((s) => (typeof s === "string" ? { text: s } : s)),
		props.tools
	)
);
const view = computed(() => {
	const working =
		["preparing", "ready", "dispatching", "streaming", "recovering"].includes(
			props.turnState || props.message.turn_state
		) ||
		props.message.streaming ||
		props.tools.some((t) => ["running", "started"].includes(t.tool_status));
	if (working && !props.message.content)
		return {
			mode: "live",
			...liveBox({
				steps: items.value
					.filter((item) => item.kind === "step")
					.map((item) => ({ text: item.text })),
				waiting: true,
				idleText: __("Working"),
			}),
		};
	const head = foldedHead({
		toolNames: turnToolNames(props.tools, []),
		failed:
			(props.turnState || props.message.turn_state) === "errored" ||
			!!props.message.error_headline,
		stopped:
			!!props.message.stopped ||
			(props.turnState || props.message.turn_state) === "cancelled",
		settled: !working,
	});
	if (!head && !items.value.length) return { mode: "folded", head: null };
	return {
		mode: "folded",
		head: {
			...(head || {}),
			label: working ? __("Working") : head?.label || __("Activity"),
			expandable: items.value.length > 0,
			subline: props.tools.length === 1 ? props.tools[0].tool_name : head?.subline || "",
		},
	};
});
// A tool can move from an orphan row when earlier history arrives. Preserve its
// expanded details; ordinary polling must not reopen a manually folded summary.
watch(
	() => props.tools.map((t) => t.name).join("\n"),
	() => {
		if (cache && props.tools.some((t) => cache.get(t.name)?.open)) open.value = true;
	},
	{ immediate: true }
);
</script>
<style scoped>
.jv-tool-step {
	display: flex;
	align-items: flex-start;
	gap: 8px;
	padding: 3px 10px;
	margin: 0;
	font-size: 12.5px;
	line-height: 1.5;
	color: var(--text-2);
	overflow-wrap: anywhere;
}
.jv-tool-step svg {
	flex: none;
	margin-top: 3px;
	color: var(--text-3);
}
</style>
