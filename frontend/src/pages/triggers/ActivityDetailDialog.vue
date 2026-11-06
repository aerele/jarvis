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

				<!-- live region: sits above the log block (reading order, no layout shift)
				     and stays present while the dialog has a row, so it exists before
				     its first message and the message is announced -->
				<div
					role="status"
					aria-live="polite"
					class="text-sm"
					:class="[
						loadError ? 'text-ink-red-4' : 'text-ink-gray-5',
						reserveStatusSlot ? 'mt-2 min-h-5' : '',
					]"
				>
					{{ statusText }}
				</div>

				<!-- full detail: mono pre-wrap block -->
				<pre
					v-if="shownDetail"
					role="region"
					aria-label="Full log"
					tabindex="0"
					:aria-busy="loading"
					class="mt-4 max-h-72 overflow-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere] rounded bg-surface-gray-2 p-3 font-mono text-sm text-ink-gray-8 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
					>{{ shownDetail }}</pre
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
							row.event_user || "System"
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
// Most of it renders from the list row itself. `detail` is optional:
// list_activity_page returns it (clipped to 4000 chars) only to trigger
// managers (System Manager / Jarvis Admin); other users never receive it, so
// its mono block appears only when the row has it. When the row carries
// `detail_truncated` (managers only), opening the dialog fetches the full
// stored log with get_activity_detail and swaps it in. The clipped text stays
// if the fetch fails or returns nothing longer. One request per open; a late
// response is dropped.
import { computed, nextTick, ref, watch } from "vue";
import { Badge, Dialog, FeatherIcon, Tooltip } from "frappe-ui";
import { timeAgo, exactDate } from "@/utils/datetime";
import { getActivityDetail } from "@/api/triggers";

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

const LOADING_TEXT = "Loading the full log\u2026";
const DONE_TEXT = "Showing the full log.";
const ERROR_TEXT =
	"Could not load the full log. The text below is cut short. Close and open this row to try again.";

const fullDetail = ref("");
const loading = ref(false);
const loadError = ref(false);
const statusText = ref("");
// A cut row that will fetch keeps the same margin and a fixed min height for
// the whole time it is shown (loading, done, error), so the rows below do not
// move when the status text changes. Other rows: the live region takes no space.
const reserveStatusSlot = computed(
	() => !!(props.row && props.row.name && props.row.detail_truncated)
);
const shownDetail = computed(() => fullDetail.value || (props.row && props.row.detail) || "");

// Per-instance request counter (not a name compare: the dialog stays mounted
// and only `row` changes). Every open, row change and close bumps it, so a
// response from an older request never lands.
let requestCounter = 0;

// The live-region text is set after a tick so the first message is announced.
function setStatus(text, seq) {
	nextTick(() => {
		if (seq === requestCounter) statusText.value = text;
	});
}

// ONE watcher: both surfaces set `row` and open in the same tick, so a single
// source of truth keeps this at exactly one fetch per open. State resets on
// open, not on close (the Dialog has a leave transition; clearing on close
// would make the text jump while it animates out).
watch(
	[() => props.modelValue, () => props.row && props.row.name],
	([open]) => {
		const seq = ++requestCounter;
		if (!open) return;
		fullDetail.value = "";
		loading.value = false;
		loadError.value = false;
		statusText.value = "";
		const row = props.row;
		if (!row || !row.name || !row.detail_truncated) return;
		const clipped = row.detail || "";
		// the list copy ends with "\u2026" when cut; the full text must be longer than the rest
		const clippedBase = clipped.endsWith("\u2026") ? clipped.slice(0, -1) : clipped;
		loading.value = true;
		setStatus(LOADING_TEXT, seq);
		getActivityDetail(row.name).then(
			(res) => {
				// `!props.modelValue` is defensive: the counter bump on close already drops
				// a stale response.
				if (seq !== requestCounter || !props.modelValue) return;
				loading.value = false;
				const full = res && res.detail;
				if (!full || full.length <= clippedBase.length) {
					// empty or not longer than what is already shown: keep the clipped text
					loadError.value = true;
					setStatus(ERROR_TEXT, seq);
					return;
				}
				fullDetail.value = full;
				setStatus(DONE_TEXT, seq);
			},
			(err) => {
				if (seq !== requestCounter || !props.modelValue) return;
				console.warn("get_activity_detail failed", row.name, err);
				loading.value = false;
				loadError.value = true;
				setStatus(ERROR_TEXT, seq);
			}
		);
	},
	{ immediate: true }
);

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
