<template>
	<!-- One macro of another user, to look at. Nothing here edits, saves or runs it:
	     an admin does not act as the owner. A second dialog over Settings (as
	     TokenLimitDialog is), so Escape closes this one alone and focus returns to
	     the row that opened it. -->
	<Dialog v-model="show" :options="{ title, size: '3xl' }">
		<template #body-content>
			<div
				v-if="error"
				class="flex flex-col items-center gap-3 py-8 text-center"
				role="alert"
			>
				<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
				<span class="text-base text-ink-gray-6">{{ error }}</span>
				<Button
					variant="subtle"
					label="Try again"
					iconLeft="refresh-cw"
					:loading="loading"
					@click="load"
				/>
			</div>

			<p v-else-if="!macro" class="text-p-base text-ink-gray-6" role="status">Loading…</p>

			<div v-else class="flex max-h-[65vh] flex-col gap-5 overflow-y-auto pr-1">
				<p class="text-p-sm text-ink-gray-6">
					Read-only. Only {{ ownerText }} can edit or run this macro.
				</p>

				<dl class="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-2 text-sm">
					<dt class="text-ink-gray-5">Owner</dt>
					<dd class="text-ink-gray-8">{{ ownerText }}</dd>
					<dt class="text-ink-gray-5">State</dt>
					<dd class="flex flex-wrap items-center gap-1">
						<Badge
							variant="subtle"
							:theme="macro.enabled ? 'green' : 'gray'"
							:label="macro.enabled ? 'On' : 'Off'"
						/>
						<Badge
							v-if="macro.skip_confirmation"
							variant="subtle"
							theme="orange"
							label="Armed"
						/>
						<span v-if="macro.skip_confirmation" class="text-xs text-ink-gray-5">
							Its runs write without a confirmation card.
						</span>
					</dd>
					<dt class="text-ink-gray-5">Schedule</dt>
					<dd class="text-ink-gray-8">
						{{ macro.schedule_enabled ? scheduleLabel(macro) : "Not scheduled" }}
						<span v-if="nextRun" class="text-ink-gray-5" :title="nextRun.hint">
							· Next: {{ nextRun.text }}
						</span>
					</dd>
					<template v-if="macro.description">
						<dt class="text-ink-gray-5">Description</dt>
						<dd class="whitespace-pre-wrap text-ink-gray-8">
							{{ macro.description }}
						</dd>
					</template>
				</dl>

				<section aria-labelledby="jv-macro-admin-summary">
					<h3
						id="jv-macro-admin-summary"
						class="mb-1 text-sm font-medium text-ink-gray-7"
					>
						Summarized prompt
					</h3>
					<pre v-if="macro.merged_prompt" class="jv-macro-admin-text" :class="PRE">{{
						macro.merged_prompt
					}}</pre>
					<p v-else class="text-sm text-ink-gray-5">
						{{
							macro.merge_status === "pending"
								? "Being summarized. It runs once the summary is ready."
								: "None. It runs its steps one by one."
						}}
					</p>
				</section>

				<section aria-labelledby="jv-macro-admin-steps">
					<h3 id="jv-macro-admin-steps" class="mb-1 text-sm font-medium text-ink-gray-7">
						Steps ({{ macro.steps.length }})
					</h3>
					<ol class="flex flex-col gap-3">
						<li v-for="(step, i) in macro.steps" :key="i">
							<div class="mb-1 text-xs text-ink-gray-5">
								{{ stepTitle(step, i) }}
							</div>
							<pre class="jv-macro-admin-text" :class="PRE">{{ step.prompt }}</pre>
						</li>
					</ol>
				</section>

				<section aria-labelledby="jv-macro-admin-runs">
					<h3 id="jv-macro-admin-runs" class="mb-1 text-sm font-medium text-ink-gray-7">
						Recent runs
					</h3>
					<p v-if="!macro.runs.length" class="text-sm text-ink-gray-5">
						It has not run yet.
					</p>
					<table v-else class="w-full text-left text-sm">
						<thead class="text-xs text-ink-gray-5">
							<tr>
								<th scope="col" class="py-1 pr-3 font-medium">Started</th>
								<th scope="col" class="py-1 pr-3 font-medium">Status</th>
								<th scope="col" class="py-1 pr-3 font-medium">Trigger</th>
								<th scope="col" class="py-1 pr-3 font-medium">Steps</th>
								<th scope="col" class="py-1 font-medium">Reason</th>
							</tr>
						</thead>
						<tbody>
							<tr
								v-for="run in macro.runs"
								:key="run.name"
								class="border-t align-top"
							>
								<td
									class="whitespace-nowrap py-1.5 pr-3 text-ink-gray-8"
									:title="exactDate(run.started_at || run.creation)"
								>
									{{ timeAgo(run.started_at || run.creation) }}
								</td>
								<td class="whitespace-nowrap py-1.5 pr-3">
									<Badge
										variant="subtle"
										:theme="RUN_THEMES[run.status] || 'gray'"
										:label="statusLabel(run.status)"
									/>
								</td>
								<td
									class="whitespace-nowrap py-1.5 pr-3 capitalize text-ink-gray-8"
								>
									{{ run.trigger || "manual" }}
								</td>
								<td class="whitespace-nowrap py-1.5 pr-3 text-ink-gray-8">
									{{ (run.current_step || 0) + "/" + (run.total_steps || 0) }}
								</td>
								<td class="whitespace-pre-wrap break-words py-1.5 text-ink-gray-7">
									{{ run.error || "-" }}
								</td>
							</tr>
						</tbody>
					</table>
				</section>
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end">
				<Button label="Close" @click="show = false" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// The read-only view of one macro for a Jarvis Admin: its settings, its steps, its
// summarized prompt, and its recent runs as status, trigger, times, step counts
// and the reason each ended. The server never sends a run's conversation, so there
// is no "Open chat" here. Every value is rendered as text ({{ }}), never as HTML:
// the prompts are another user's free text.
import { computed, ref, watch } from "vue";
import { Badge, Button, Dialog, FeatherIcon } from "frappe-ui";
import { adminGetMacro } from "@/api/macrosAdmin";
import { errMessage } from "@/lib/errors";
import { nextRunCell } from "@/lib/macroSchedule";
import { scheduleLabel } from "@/pages/macros/scheduleLabel";
import { timeAgo, exactDate, toLocalMs } from "@/utils/datetime";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	name: { type: String, required: true },
});
const emit = defineEmits(["update:modelValue"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const PRE =
	"max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-gray-2 p-3 text-xs text-ink-gray-8";
// The Runs tab's own colours and words for a run's status.
const RUN_THEMES = {
	queued: "gray",
	running: "blue",
	waiting_capacity: "orange",
	completed: "green",
	failed: "red",
	stopped: "gray",
};
const STATUS_LABELS = { waiting_capacity: "Waiting for capacity" };
function statusLabel(s) {
	if (!s) return "";
	return STATUS_LABELS[s] || s.charAt(0).toUpperCase() + s.slice(1);
}

const macro = ref(null);
const loading = ref(false);
const error = ref("");

// Only the newest request may fill the dialog: it is re-pointed at another row
// without being unmounted.
let latest = 0;
async function load() {
	const mine = ++latest;
	loading.value = true;
	error.value = "";
	try {
		const res = (await adminGetMacro(props.name)) || {};
		if (mine !== latest) return;
		macro.value = { ...res, steps: res.steps || [], runs: res.runs || [] };
	} catch (e) {
		if (mine !== latest) return;
		macro.value = null;
		error.value = errMessage(e, "Could not load this macro.");
	} finally {
		if (mine === latest) loading.value = false;
	}
}

watch(
	() => [props.modelValue, props.name],
	([open]) => {
		if (!open) return;
		macro.value = null;
		load();
	},
	{ immediate: true }
);

const title = computed(() => (macro.value && macro.value.macro_name) || "Macro");
const ownerText = computed(() => {
	const m = macro.value || {};
	return m.owner_full_name && m.owner_full_name !== m.owner
		? `${m.owner_full_name} (${m.owner})`
		: m.owner || "its owner";
});
const nextRun = computed(() => {
	const m = macro.value;
	if (!m) return null;
	return nextRunCell({
		scheduleEnabled: !!m.schedule_enabled,
		enabled: !!m.enabled,
		nextRunMs: toLocalMs(m.next_run_at),
		nowMs: Date.now(),
		when: exactDate(m.next_run_at),
		relative: timeAgo(m.next_run_at),
		isRetry: !!m.next_run_is_retry,
	});
});

// A step's heading: its number and label, then what it runs with besides its
// prompt, in a few words ("2. Send the report · model gpt-5.5, 1 skill").
function stepTitle(step, i) {
	const parts = [];
	if (step.model_override) parts.push(`model ${step.model_override}`);
	if (step.thinking_override) parts.push(`thinking ${step.thinking_override}`);
	const n = Number(step.skill_count) || 0;
	if (n) parts.push(`${n} skill${n === 1 ? "" : "s"}`);
	const title = `${i + 1}. ${step.label || "Step " + (i + 1)}`;
	return parts.length ? `${title} · ${parts.join(", ")}` : title;
}
</script>
