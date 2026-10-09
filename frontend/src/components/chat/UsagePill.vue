<template>
	<Popover v-if="reading.state !== 'none'" placement="top-end">
		<template #target="{ togglePopover }">
			<button
				type="button"
				data-testid="usage-pill"
				class="jv-usage-pill"
				:class="{
					'jv-usage-warn': reading.state === 'warn',
					'jv-usage-full': reading.state === 'full',
				}"
				:title="title"
				:aria-label="title"
				@click="togglePopover()"
			>
				<span class="jv-usage-track" aria-hidden="true">
					<span
						data-testid="usage-fill"
						class="jv-usage-fill"
						:style="{ width: reading.pct + '%' }"
					/>
				</span>
			</button>
		</template>
		<template #body>
			<div
				class="my-2 w-64 rounded-lg bg-surface-modal p-3 shadow-2xl ring-1 ring-black ring-opacity-5"
			>
				<div class="flex items-baseline justify-between">
					<h3 class="text-base font-semibold text-ink-gray-9">
						{{ brand.agentName }} usage limit
					</h3>
					<span class="text-p-sm text-ink-gray-6">{{ reading.pct }}%</span>
				</div>
				<!-- Named so nobody reads it as their ChatGPT / API-provider usage. -->
				<p class="mt-1 text-p-sm text-ink-gray-5">
					Your allowance in {{ brand.agentName }}, set by your admin. Not your model
					provider's usage.
				</p>
				<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
					<div
						class="h-full rounded-full"
						:class="barClass"
						:style="{ width: reading.pct + '%' }"
					/>
				</div>
				<div class="mt-2">
					<KvRow
						:label="windowLabel"
						:value="`${fmtTokens(reading.used)} of ${fmtTokens(reading.limit)}`"
					/>
					<KvRow v-if="resetLabel" label="Resets" :value="resetLabel" />
					<KvRow
						v-if="reading.label !== 'all time'"
						label="All time"
						:value="fmtTokens(usage.total_tokens)"
					/>
				</div>
				<p v-if="reading.state !== 'quiet'" class="mt-3 text-p-sm text-ink-gray-5">
					Ask your {{ brand.agentName }} admin to raise it.
				</p>
			</div>
		</template>
	</Popover>
</template>

<script setup>
// The member's own token cap, next to the context ring (design pick 2,
// 2026-09-08): quiet under 80%, amber from 80%, red when the window is full.
// Hidden entirely when no cap is set. Same popover language as ContextRing.
import { computed } from "vue";
import { Popover } from "frappe-ui";
import KvRow from "@/components/settings/KvRow.vue";
import { fmtTokens, limitReading } from "@/lib/tokens";
import { brand } from "@/branding";

const props = defineProps({
	usage: { type: Object, default: null },
});

const reading = computed(() => limitReading(props.usage));

const RESET_TIME = { midnight: "Midnight", Sunday: "Sunday 00:00", "the 1st": "1st of the month" };
const resetLabel = computed(() => RESET_TIME[reading.value.reset] || "");
const windowLabel = computed(() => {
	const l = reading.value.label;
	return l ? l.charAt(0).toUpperCase() + l.slice(1) : "Used";
});
// The bar carries no text (design note 2026-09-08: the composer was crowded);
// the reading lives in the tooltip, the accessible name and the popover.
const title = computed(() => {
	const r = reading.value;
	if (r.state === "full") return r.reset ? `Limit reached · resets ${r.reset}` : "Limit reached";
	return `${fmtTokens(r.used)} of ${fmtTokens(r.limit)} ${r.label}`;
});
const barClass = computed(() =>
	reading.value.state === "full"
		? "bg-surface-red-5"
		: reading.value.state === "warn"
		? "jv-usage-bar-warn"
		: "bg-surface-gray-7"
);
</script>

<style scoped>
/* Same 18px footprint as the context ring beside it: a 28x4 track, no text. */
.jv-usage-pill {
	display: inline-flex;
	align-items: center;
	height: 18px;
	padding: 0 2px;
	border: 0;
	border-radius: 6px;
	background: transparent;
	cursor: pointer;
}
.jv-usage-pill:hover {
	background: var(--surface-2);
}
.jv-usage-pill:focus-visible {
	outline: 2px solid var(--jv-terra);
	outline-offset: 2px;
}
.jv-usage-track {
	display: block;
	width: 28px;
	height: 4px;
	border-radius: 999px;
	background: color-mix(in srgb, var(--text-3) 45%, transparent);
	overflow: hidden;
}
.jv-usage-fill {
	display: block;
	height: 100%;
	border-radius: 999px;
	background: linear-gradient(90deg, #6e8bff, #8b5cf6);
	transition: width 300ms ease;
}
.jv-usage-warn .jv-usage-fill,
.jv-usage-bar-warn {
	background: var(--jv-terra);
}
.jv-usage-full .jv-usage-fill {
	background: #dc2626;
}
@media (prefers-reduced-motion: reduce) {
	.jv-usage-fill {
		transition: none;
	}
}
</style>
