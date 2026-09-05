<template>
	<Popover v-if="context && context.fresh" placement="top-end">
		<template #target="{ togglePopover }">
			<button
				type="button"
				data-testid="context-ring"
				class="jv-ctx-ring"
				:class="{
					'jv-ctx-warn': warn,
					'jv-ctx-busy': compacting,
					'jv-ctx-done': compacted,
				}"
				:title="title"
				:aria-label="title"
				@click="togglePopover()"
			>
				<svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true">
					<defs>
						<linearGradient :id="gradId" x1="0" y1="0" x2="1" y2="1">
							<stop offset="0%" stop-color="#6e8bff" />
							<stop offset="100%" stop-color="#8b5cf6" />
						</linearGradient>
					</defs>
					<circle
						cx="10"
						cy="10"
						r="7.5"
						fill="none"
						stroke="var(--surface-3)"
						stroke-width="2.6"
					/>
					<circle
						v-if="!compacted"
						class="jv-ctx-fillarc"
						:class="{ 'jv-ctx-spin': compacting }"
						cx="10"
						cy="10"
						r="7.5"
						fill="none"
						:stroke="warn ? 'var(--jv-terra)' : `url(#${gradId})`"
						stroke-width="2.6"
						stroke-linecap="round"
						:stroke-dasharray="compacting ? '22 47.12' : dashArray"
					/>
					<path
						v-if="compacted"
						d="M6.5 10.2l2.3 2.3 4.7-4.8"
						fill="none"
						stroke="var(--ok, #16a34a)"
						stroke-width="1.6"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
			</button>
		</template>
		<template #body="{ close }">
			<div
				class="my-2 min-w-60 rounded-lg bg-surface-modal p-2 shadow-2xl ring-1 ring-black ring-opacity-5 w-72 p-3"
			>
				<div class="flex items-baseline justify-between">
					<h3 class="text-base font-semibold text-ink-gray-9">Context</h3>
					<span class="text-p-sm text-ink-gray-6" :class="{ 'jv-ctx-warn-text': warn }">
						{{ compacting ? "Compacting…" : `${pct}% of ${fmtTokens(capacity)}` }}
					</span>
				</div>

				<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
					<div
						class="h-full rounded-full"
						:class="warn ? 'jv-ctx-bar-warn' : 'bg-surface-gray-7'"
						:style="{ width: (compacted ? 0 : pct) + '%' }"
					/>
				</div>

				<div class="mt-2">
					<KvRow label="In use" :value="inUseLabel" />
					<KvRow
						label="Auto-compacts at"
						:value="`${fmtTokens(autoTokens)} (${autoCompactPct}%)`"
					/>
					<KvRow
						v-if="lastIn || lastOut"
						label="Last reply"
						:value="`${fmtTokens(lastIn)} in · ${fmtTokens(lastOut)} out`"
					/>
					<KvRow label="Compacted" :value="compactedLabel" />
				</div>

				<div class="mt-3 flex justify-end">
					<Button
						variant="solid"
						size="sm"
						label="Compact"
						:disabled="compacting"
						@click="
							close();
							$emit('compact');
						"
					/>
				</div>
			</div>
		</template>
	</Popover>
</template>

<script>
// Module-scope counter (shared across instances, unlike anything declared in
// <script setup>) so two mounted rings never collide on the same gradient id.
let gradSeq = 0;
</script>

<script setup>
// ContextRing replaces ContextPill (#1136, design variant A): an 18px ring
// with no text and no tick line - details move into a click-to-open popover.
// Same fill-gradient / warn / busy / done language the pill used, just drawn
// as an SVG arc instead of a bar.
import { computed } from "vue";
import { Popover, Button } from "frappe-ui";
import KvRow from "@/components/settings/KvRow.vue";
import { fmtTokens } from "@/lib/tokens";
import { timeAgo } from "@/utils/datetime";

const props = defineProps({
	context: { type: Object, default: null },
	compacting: { type: Boolean, default: false },
	compacted: { type: Boolean, default: false },
});
defineEmits(["compact"]);

const gradId = `jv-ctx-grad-${++gradSeq}`;

const used = computed(() => Number(props.context?.used || 0));
const capacity = computed(() => Number(props.context?.capacity || 0));
const warnPct = computed(() => Number(props.context?.warn_pct ?? 80));
const autoCompactPct = computed(() => Number(props.context?.auto_compact_pct || 0));
const autoTokens = computed(() => Math.round((capacity.value * autoCompactPct.value) / 100));
const pct = computed(() =>
	Math.round(Math.min(100, Math.max(0, Number(props.context?.pct || 0))))
);
const warn = computed(() => !!props.context && pct.value >= warnPct.value);
const lastIn = computed(() => Number(props.context?.last_in || 0));
const lastOut = computed(() => Number(props.context?.last_out || 0));
const compactionCount = computed(() => Number(props.context?.compaction_count || 0));
const lastCompactedAt = computed(() => props.context?.last_compacted_at || null);

// Fraction of the 47.12 (2*pi*7.5) circumference to fill, clockwise from the
// -90deg (12 o'clock) start baked into the .jv-ctx-fillarc CSS transform.
const dashArray = computed(() => `${((pct.value / 100) * 47.12).toFixed(2)} 47.12`);

const inUseLabel = computed(() =>
	props.compacted ? "Compacted, measuring on next reply" : `${fmtTokens(used.value)} tokens`
);

const compactedLabel = computed(() =>
	compactionCount.value > 0
		? `${compactionCount.value}×, ${timeAgo(lastCompactedAt.value)}`
		: "never"
);

const title = computed(() => {
	if (props.compacting) return "Compacting this chat";
	if (props.compacted) return "Context compacted. The meter updates after your next message.";
	return `${fmtTokens(used.value)} of ${fmtTokens(capacity.value)} (${
		pct.value
	}%). Context in use.`;
});
</script>

<style>
/* Unscoped on purpose: frappe-ui's Popover teleports the #body slot out of
   this component's DOM subtree (reka-ui PopoverPortal), so a scoped custom
   property - only inherited within our own tree - would not reach it, and
   .jv-dark (ChatView's root class) is not an ancestor of the teleported
   node either. `data-theme` on <html> (set by theme.js's applyTheme,
   ChatView's own bridge to frappe-ui) IS an ancestor of any teleported
   content, so key the override on that instead of prefers-color-scheme -
   this follows the theme the user actually picked, not the OS setting
   underneath it. .jv-dark is kept alongside it for the ring itself, which
   never leaves ChatView's tree. */
:root {
	--jv-terra: #c9623f;
}
:root[data-theme="dark"],
.jv-dark {
	--jv-terra: #e08a66;
}
</style>

<style scoped>
.jv-ctx-ring {
	width: 30px;
	height: 30px;
	display: grid;
	place-items: center;
	border: 0;
	background: transparent;
	border-radius: 8px;
	cursor: pointer;
}
.jv-ctx-ring:hover {
	background: var(--surface-3);
}
.jv-ctx-ring:focus-visible {
	outline: 2px solid var(--brand-1);
	outline-offset: 1px;
}
.jv-ctx-fillarc {
	transform-origin: 10px 10px;
	transform: rotate(-90deg);
}
.jv-ctx-fillarc.jv-ctx-spin {
	animation: jv-ctx-spin 1.1s linear infinite;
}
@keyframes jv-ctx-spin {
	from {
		transform: rotate(-90deg);
	}
	to {
		transform: rotate(270deg);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-ctx-fillarc.jv-ctx-spin {
		animation: none;
	}
}
.jv-ctx-warn-text {
	color: var(--jv-terra);
}
.jv-ctx-bar-warn {
	background: var(--jv-terra);
}
</style>
