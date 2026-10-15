<template>
	<div v-if="rows.length" class="flex w-full flex-col gap-2">
		<div class="flex items-center justify-between gap-3">
			<span class="text-[11px] font-semibold uppercase tracking-wide text-ink-gray-5">{{
				title
			}}</span>
			<Button
				size="sm"
				variant="subtle"
				icon-left="copy"
				:label="copied ? 'Copied' : copyLabel"
				@click="copy"
			/>
		</div>
		<div
			class="min-w-0 overflow-x-auto rounded-lg border border-outline-gray-1"
			tabindex="0"
			:aria-label="`${title} (scrolls sideways on a narrow screen)`"
		>
			<!-- min-width as an inline style rather than a fixed-width Tailwind class: the mobile guard
				 (tests/mobile-responsive.spec.js) scans source text for those, and this width
				 genuinely lives inside its own horizontal scroller. -->
			<div ref="tableEl" :style="{ minWidth: variant === 'admin' ? '460px' : '560px' }" role="table" :aria-label="title">
				<div
					class="grid gap-3 border-b border-outline-gray-1 bg-surface-gray-1 px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-gray-5"
					:style="gridStyle"
					role="row"
				>
					<span v-for="c in columns" :key="c" role="columnheader">{{ c }}</span>
				</div>
				<div
					v-for="(row, i) in rows"
					:key="i"
					class="grid items-center gap-3 px-4 py-3 text-p-sm text-ink-gray-7"
					:class="i < rows.length - 1 ? 'border-b border-outline-gray-1' : ''"
					:style="gridStyle"
					role="row"
				>
					<span class="inline-flex items-center gap-1.5" role="cell">
						<FeatherIcon
							:name="row.direction === 'Outbound' ? 'arrow-up-right' : 'arrow-down-left'"
							class="h-3.5 w-3.5 text-ink-gray-5"
							aria-hidden="true"
						/>{{ row.direction }}
					</span>
					<span
						v-if="variant === 'server'"
						role="cell"
						:class="row.fromMono ? 'font-mono text-ink-gray-9' : ''"
						>{{ row.from }}</span
					>
					<span class="flex min-w-0 flex-col gap-0.5" role="cell">
						<span :class="row.toMono ? 'font-mono text-ink-gray-9' : ''">{{ row.to }}</span>
						<span v-if="row.toNote" class="text-p-xs text-ink-gray-5">{{ row.toNote }}</span>
					</span>
					<span role="cell">{{ row.port }}</span>
				</div>
			</div>
		</div>
		<span v-if="hint" class="text-p-xs text-ink-gray-5">{{ hint }}</span>
		<span class="sr-only" role="status">{{ copied ? "Copied" : "" }}</span>
	</div>
</template>

<script setup>
// The rules the customer's network team has to add, one table for both places that show
// them: AllowAdmin (one outbound rule to the Jarvis setup server) and AllowServer / the
// Connect panel (outbound to the held host, inbound from it). Tailwind utilities only - the
// view's scoped .ob-* CSS does not reach into a child component.
import { computed, onBeforeUnmount, ref } from "vue";
import { Button, FeatherIcon } from "frappe-ui";

import { copyText } from "@/lib/actionSummary.js";
import { rulesToText } from "./privateNetwork.js";

const props = defineProps({
	rows: { type: Array, required: true },
	variant: { type: String, default: "server" }, // "admin" | "server"
	title: { type: String, default: "Rules for your network team" },
	copyLabel: { type: String, default: "Copy rules" },
});

const columns = computed(() =>
	props.variant === "admin"
		? ["Direction", "Destination", "Port"]
		: ["Direction", "From", "To", "Port"]
);
const gridStyle = computed(() => ({
	gridTemplateColumns:
		props.variant === "admin"
			? "120px minmax(0, 1fr) 130px"
			: "110px minmax(0, 1fr) minmax(0, 1fr) 80px",
}));

const copied = ref(false);
const hint = ref("");
const tableEl = ref(null);
let timer = null;
async function copy() {
	hint.value = "";
	if (!(await copyText(rulesToText(props.rows, props.variant)))) {
		selectTable();
		hint.value = "Select and copy";
		return;
	}
	copied.value = true;
	clearTimeout(timer);
	timer = setTimeout(() => (copied.value = false), 2000);
}
// Clipboard blocked or missing: select the rules so the person can copy them by hand.
function selectTable() {
	const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
	if (!tableEl.value || !sel) return;
	const range = document.createRange();
	range.selectNodeContents(tableEl.value);
	sel.removeAllRanges();
	sel.addRange(range);
}
onBeforeUnmount(() => clearTimeout(timer));
</script>
