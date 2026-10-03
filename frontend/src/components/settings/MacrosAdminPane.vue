<template>
	<SettingsPane
		title="Macros"
		description="Every user's macros on this site. Open one to read it, or stop a run. Only its owner can edit or run a macro."
	>
		<template #actions>
			<Button
				variant="subtle"
				iconLeft="refresh-cw"
				label="Refresh"
				:loading="loading"
				@click="load({ keep: true })"
			/>
		</template>

		<div class="flex flex-col gap-4">
			<!-- Filters. Each control carries its own visible label: four selects
			     side by side that all read "All" could not be told apart. -->
			<div class="flex flex-wrap items-end gap-2">
				<FormControl
					class="min-w-40 flex-1"
					type="text"
					label="Search"
					placeholder="Macro or owner"
					:modelValue="search"
					@update:modelValue="onSearch"
				/>
				<FormControl
					class="w-40"
					type="select"
					label="Owner"
					:options="ownerOptions"
					:modelValue="filters.owner"
					@update:modelValue="(v) => setFilter('owner', v)"
				/>
				<FormControl
					class="w-32"
					type="select"
					label="Armed"
					:options="ARMED_OPTIONS"
					:modelValue="filters.armed"
					@update:modelValue="(v) => setFilter('armed', v)"
				/>
				<FormControl
					class="w-32"
					type="select"
					label="Schedule"
					:options="SCHEDULE_OPTIONS"
					:modelValue="filters.scheduled"
					@update:modelValue="(v) => setFilter('scheduled', v)"
				/>
				<FormControl
					class="w-32"
					type="select"
					label="Runs"
					:options="LIVE_OPTIONS"
					:modelValue="filters.live_run"
					@update:modelValue="(v) => setFilter('live_run', v)"
				/>
			</div>

			<!-- Nothing to show and the load failed: say so, with a way to retry. -->
			<div
				v-if="error && !rows.length"
				class="flex flex-col items-center gap-3 py-12 text-center"
				role="alert"
			>
				<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
				<span class="text-base text-ink-gray-6">{{ error }}</span>
				<Button
					variant="subtle"
					label="Try again"
					iconLeft="refresh-cw"
					:loading="loading"
					@click="load()"
				/>
			</div>

			<p v-else-if="!loaded" class="text-p-base text-ink-gray-6" role="status">Loading…</p>

			<div
				v-else-if="!rows.length"
				class="flex flex-col items-center gap-3 py-12 text-center"
				role="status"
			>
				<FeatherIcon name="layers" class="size-8 text-ink-gray-4" />
				<span class="text-base text-ink-gray-6">
					{{
						filtering
							? "No macros match these filters."
							: "Nobody has made a macro yet."
					}}
				</span>
				<Button
					v-if="filtering"
					variant="subtle"
					label="Clear filters"
					@click="clearFilters"
				/>
			</div>

			<template v-else>
				<!-- A refresh that failed keeps the rows it had: they are still the last
				     thing the server said. -->
				<div
					v-if="error"
					class="flex items-center justify-between gap-3 rounded-md bg-surface-amber-2 px-3 py-2"
					role="alert"
				>
					<span class="text-sm text-ink-gray-8">
						{{ error }} Showing what was loaded before.
					</span>
					<Button
						size="sm"
						label="Try again"
						:loading="loading"
						@click="load({ keep: true })"
					/>
				</div>

				<div>
					<div
						class="grid items-center gap-3.5 pb-2 text-xs font-medium text-ink-gray-5"
						:style="GRID"
					>
						<div>Macro</div>
						<div>Owner</div>
						<div>Schedule</div>
						<div>Last run</div>
						<div class="sr-only">Actions</div>
					</div>

					<div
						v-for="row in rows"
						:key="row.name"
						class="jv-macro-admin-row grid items-center gap-3.5 border-t py-3"
						:style="GRID"
					>
						<div class="min-w-0">
							<button
								type="button"
								class="jv-macro-admin-open max-w-full truncate rounded text-left text-sm font-medium text-ink-gray-9 underline-offset-2 hover:underline focus-visible:underline"
								:aria-label="`Open ${
									row.macro_name || row.name
								}, owned by ${ownerText(row)}. Read-only.`"
								@click="open(row)"
							>
								{{ row.macro_name || row.name }}
							</button>
							<!-- Words, not colour alone, carry each state. -->
							<div class="mt-1 flex flex-wrap items-center gap-1">
								<Badge
									variant="subtle"
									:theme="row.enabled ? 'green' : 'gray'"
									:label="row.enabled ? 'On' : 'Off'"
								/>
								<Badge
									v-if="row.skip_confirmation"
									variant="subtle"
									theme="orange"
									label="Armed"
								/>
							</div>
						</div>

						<div class="min-w-0">
							<div class="truncate text-sm text-ink-gray-8">
								{{ row.owner_full_name || row.owner }}
							</div>
							<div
								v-if="row.owner_full_name && row.owner_full_name !== row.owner"
								class="truncate text-xs text-ink-gray-5"
							>
								{{ row.owner }}
							</div>
						</div>

						<div class="min-w-0">
							<template v-if="row.schedule_enabled">
								<div class="truncate text-sm text-ink-gray-8">
									{{ scheduleLabel(row) }}
								</div>
								<div
									v-if="nextRun(row)"
									class="truncate text-xs"
									:class="nextRun(row).class || 'text-ink-gray-5'"
									:title="nextRun(row).hint"
								>
									Next: {{ nextRun(row).text }}
								</div>
							</template>
							<span v-else class="text-sm text-ink-gray-5">Not scheduled</span>
						</div>

						<div class="min-w-0">
							<div
								v-if="lastRun(row)"
								class="truncate text-sm"
								:class="lastRun(row).class || 'text-ink-gray-8'"
								:title="lastRun(row).hint"
							>
								{{ lastRun(row).text }}
							</div>
							<span v-else class="text-sm text-ink-gray-5">Never ran</span>
						</div>

						<div class="flex justify-end">
							<Button
								v-if="row.live_run"
								class="jv-macro-admin-stop"
								size="sm"
								variant="subtle"
								theme="red"
								label="Stop run"
								:loading="stopping === row.name"
								:disabled="!!stopping"
								:aria-label="`Stop the run of ${
									row.macro_name || row.name
								}, owned by ${ownerText(row)}`"
								@click="stop(row)"
							/>
						</div>
					</div>
				</div>

				<div class="flex items-center justify-between gap-3">
					<span class="text-xs text-ink-gray-5" aria-live="polite">
						Showing {{ rows.length }} of {{ total }}
					</span>
					<Button
						v-if="hasMore"
						variant="subtle"
						label="Load more"
						:loading="loading"
						@click="load({ append: true })"
					/>
				</div>
			</template>
		</div>

		<!-- The macro, to look at: a second dialog over Settings, not a route. Escape
		     closes it alone and focus goes back to the row's button. -->
		<MacroAdminDialog v-if="opened" v-model="detailOpen" :name="opened" />
	</SettingsPane>
</template>

<script setup>
// A Jarvis Admin's view of every user's macros: who owns each, whether it is on,
// scheduled or armed, how its last run went. One action, Stop run. Opening a row
// shows the macro read-only (MacroAdminDialog).
//
// Gated at the SettingsDialog level by window.is_jarvis_admin. The server checks
// the Jarvis Admin role itself on every call (jarvis.chat.macros_admin_api), so a
// stale client gate can only hide the nav item.
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { Badge, Button, FeatherIcon, FormControl, toast } from "frappe-ui";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import MacroAdminDialog from "@/components/settings/MacroAdminDialog.vue";
import { useConfirm } from "@/composables/useConfirm";
import { adminListMacros, adminStopRun } from "@/api/macrosAdmin";
import { errMessage, errHtml, escapeHtml } from "@/lib/errors";
import { lastRunCell } from "@/lib/macroRunOutcome";
import { nextRunCell } from "@/lib/macroSchedule";
import { scheduleLabel } from "@/pages/macros/scheduleLabel";
import { timeAgo, exactDate, toLocalMs } from "@/utils/datetime";

const PAGE = 20;
// A refresh re-reads what is on screen, up to the server's page cap.
const MAX_PAGE = 100;
const SEARCH_WAIT_MS = 300;
const GRID = "grid-template-columns: 2fr 1.4fr 1.4fr 1fr 5.5rem";

const ARMED_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Armed", value: "1" },
	{ label: "Not armed", value: "0" },
];
const SCHEDULE_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Scheduled", value: "1" },
	{ label: "Not scheduled", value: "0" },
];
const LIVE_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Running now", value: "1" },
	{ label: "Not running", value: "0" },
];

const { confirm } = useConfirm();

const rows = ref([]);
const total = ref(0);
const hasMore = ref(false);
const owners = ref([]);
const loading = ref(false);
const loaded = ref(false);
const error = ref("");

const search = ref("");
const filters = reactive({ owner: "", armed: "", scheduled: "", live_run: "" });
const filtering = computed(
	() => !!search.value.trim() || Object.values(filters).some((v) => v !== "")
);
const ownerOptions = computed(() => [
	{ label: "All owners", value: "" },
	...owners.value.map((o) => ({
		label: `${o.full_name || o.user} (${o.macros})`,
		value: o.user,
	})),
]);

// Only the newest request may write to the list: a slow answer to an earlier
// filter must not replace the rows of the one chosen after it.
let latest = 0;

// `append`: the next page. `keep`: re-read as many rows as are on screen (a refresh,
// or after a stop), so the list does not jump back to its first page.
async function load({ append = false, keep = false } = {}) {
	const mine = ++latest;
	loading.value = true;
	const active = {};
	for (const [k, v] of Object.entries(filters)) if (v !== "") active[k] = v;
	try {
		const res =
			(await adminListMacros({
				search: search.value.trim(),
				filters: active,
				start: append ? rows.value.length : 0,
				pageLength: keep ? Math.min(MAX_PAGE, Math.max(PAGE, rows.value.length)) : PAGE,
			})) || {};
		if (mine !== latest) return;
		const page = res.rows || [];
		if (append) {
			// A macro made or deleted between two pages shifts the rows: never list
			// one twice.
			const seen = new Set(rows.value.map((r) => r.name));
			rows.value = [...rows.value, ...page.filter((r) => !seen.has(r.name))];
		} else {
			rows.value = page;
		}
		total.value = Number(res.total) || 0;
		hasMore.value = !!res.has_more;
		if (Array.isArray(res.owners)) owners.value = res.owners;
		error.value = "";
		loaded.value = true;
	} catch (e) {
		if (mine !== latest) return;
		error.value = errMessage(e, "Could not load the macros.");
		loaded.value = true;
	} finally {
		if (mine === latest) loading.value = false;
	}
}

function setFilter(key, value) {
	filters[key] = value == null ? "" : String(value);
	load();
}

let searchTimer = null;
function onSearch(value) {
	search.value = value == null ? "" : String(value);
	clearTimeout(searchTimer);
	searchTimer = setTimeout(load, SEARCH_WAIT_MS);
}

function clearFilters() {
	clearTimeout(searchTimer);
	search.value = "";
	for (const k of Object.keys(filters)) filters[k] = "";
	load();
}

// ── cells (the same decisions the owner's Macros list makes) ─────────────────
const LAST_RUN_TONE = {
	bad: "text-ink-red-4",
	warn: "text-ink-amber-3",
	muted: "text-ink-gray-5",
	info: "text-ink-blue-3",
};
const NEXT_RUN_TONE = { warn: "text-ink-amber-3", muted: "text-ink-gray-5" };

function lastRun(row) {
	const at = row.last_run && (row.last_run.finished_at || row.last_run.started_at);
	const cell = lastRunCell({
		// `null`, never undefined: undefined means "an older server", which this
		// endpoint cannot be.
		lastRun: row.last_run || null,
		when: at ? exactDate(at) : "",
		relative: at ? timeAgo(at) : "",
	});
	return cell && { ...cell, class: LAST_RUN_TONE[cell.tone] || "" };
}
function nextRun(row) {
	const cell = nextRunCell({
		scheduleEnabled: !!row.schedule_enabled,
		enabled: !!row.enabled,
		nextRunMs: toLocalMs(row.next_run_at),
		nowMs: Date.now(),
		when: exactDate(row.next_run_at),
		relative: timeAgo(row.next_run_at),
		isRetry: !!row.next_run_is_retry,
	});
	return cell && { ...cell, class: NEXT_RUN_TONE[cell.tone] || "" };
}

// "Asha Rao (asha@example.com)", or the address alone when that is all there is.
function ownerText(row) {
	const full = row.owner_full_name;
	return full && full !== row.owner ? `${full} (${row.owner})` : row.owner;
}

// ── open one, read-only ──────────────────────────────────────────────────────
const opened = ref("");
const detailOpen = ref(false);
function open(row) {
	opened.value = row.name;
	detailOpen.value = true;
}

// ── stop a run ───────────────────────────────────────────────────────────────
// The name of the row whose stop is in flight. One at a time: every Stop button
// is disabled meanwhile, and nothing is shown as stopped until the server says so.
const stopping = ref("");

async function stop(row) {
	if (stopping.value || !row.live_run) return;
	const name = row.macro_name || row.name;
	// The confirmation renders its message as text, so the names go in as they are.
	const ok = await confirm({
		title: "Stop this run?",
		message:
			`Stop the run of “${name}”, owned by ${ownerText(row)}? ` +
			"A step the assistant is already working on still finishes; no further step starts. " +
			"The owner will see that an administrator stopped it.",
		confirmLabel: "Stop run",
		danger: true,
	});
	if (!ok) return;
	stopping.value = row.name;
	try {
		const res = (await adminStopRun(row.live_run)) || {};
		// A toast renders its message as HTML: every name and server sentence is escaped.
		if (res.stopped === false) {
			toast.create({
				message: escapeHtml(res.message || "This run had already ended."),
				type: "info",
			});
		} else {
			toast.success(`Stopped the run of “${escapeHtml(name)}”`);
		}
		await load({ keep: true });
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		stopping.value = "";
	}
}

onMounted(load);
onBeforeUnmount(() => clearTimeout(searchTimer));
</script>
