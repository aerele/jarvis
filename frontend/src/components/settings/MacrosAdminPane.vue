<template>
	<SettingsPane
		title="Macros"
		description="Every user's macros on this site. Open one to read it, stop a run, put a macro on hold, hand it to another user or delete it. Only its owner can edit or run a macro."
	>
		<template #actions>
			<Button
				variant="subtle"
				iconLeft="refresh-cw"
				label="Refresh"
				:loading="loading"
				@click="refresh"
			/>
		</template>

		<div class="flex flex-col gap-4">
			<!-- Filters, on two rows on purpose: the search box has the first to itself
			     and the selects wrap under it, so nothing is squeezed at any width.
			     Each control carries its own visible label: four selects side by side
			     that all read "All" could not be told apart. -->
			<div class="flex flex-col gap-2">
				<FormControl
					class="w-full"
					type="text"
					label="Search"
					placeholder="Macro, owner's name or email"
					:modelValue="search"
					@update:modelValue="onSearch"
				/>
				<div class="flex flex-wrap items-end gap-2">
					<FormControl
						class="min-w-36 flex-1"
						type="select"
						label="Owner"
						:options="ownerOptions"
						:modelValue="filters.owner"
						@update:modelValue="(v) => setFilter('owner', v)"
					/>
					<FormControl
						class="min-w-28 flex-1"
						type="select"
						label="Armed"
						:title="ARMED_HELP"
						:options="ARMED_OPTIONS"
						:modelValue="filters.armed"
						@update:modelValue="(v) => setFilter('armed', v)"
					/>
					<FormControl
						class="min-w-28 flex-1"
						type="select"
						label="Schedule"
						:options="SCHEDULE_OPTIONS"
						:modelValue="filters.scheduled"
						@update:modelValue="(v) => setFilter('scheduled', v)"
					/>
					<FormControl
						class="min-w-28 flex-1"
						type="select"
						label="Runs"
						:options="LIVE_OPTIONS"
						:modelValue="filters.live_run"
						@update:modelValue="(v) => setFilter('live_run', v)"
					/>
					<FormControl
						class="min-w-28 flex-1"
						type="select"
						label="Hold"
						:options="HOLD_OPTIONS"
						:modelValue="filters.on_hold"
						@update:modelValue="(v) => setFilter('on_hold', v)"
					/>
					<!-- The combination an admin looks for, in one step: it sets the two
					     selects beside it, so what is filtered stays in plain sight. -->
					<Button
						class="jv-macro-admin-preset"
						:variant="armedAndScheduled ? 'solid' : 'subtle'"
						label="Armed and scheduled"
						:aria-pressed="armedAndScheduled ? 'true' : 'false'"
						title="Macros that write without asking for confirmation and run on a schedule"
						@click="toggleArmedAndScheduled"
					/>
				</div>
				<p class="jv-macro-admin-help text-xs text-ink-gray-5">{{ ARMED_HELP }}</p>
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
					@click="load('reset')"
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
				     thing the server said, under the filters still selected. -->
				<div
					v-if="error"
					class="flex items-center justify-between gap-3 rounded-md bg-surface-amber-2 px-3 py-2"
					role="alert"
				>
					<span class="text-sm text-ink-gray-8">
						{{ error }} Showing what was loaded before.
					</span>
					<Button size="sm" label="Try again" :loading="loading" @click="load('keep')" />
				</div>

				<!-- The list scrolls sideways inside this box when the pane is narrow;
				     the dialog and the page never do. Focusable, so the keyboard can
				     scroll it too. Table roles: a screen reader hears each value with
				     its column. -->
				<div
					ref="listEl"
					class="jv-macro-admin-scroll overflow-x-auto rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-outline-gray-3"
					tabindex="0"
					role="region"
					aria-label="Macros list"
				>
					<div role="table" aria-label="Macros" :style="GRID_MIN">
						<div
							role="row"
							class="grid items-center gap-3.5 pb-2 text-xs font-medium text-ink-gray-5"
							:style="GRID"
						>
							<div role="columnheader">Macro</div>
							<div role="columnheader">Owner</div>
							<div role="columnheader">Schedule</div>
							<div role="columnheader">Last run</div>
							<div role="columnheader"><span class="sr-only">Actions</span></div>
						</div>

						<div
							v-for="row in rows"
							:key="row.name"
							role="row"
							class="jv-macro-admin-row grid items-center gap-3.5 border-t py-3"
							:style="GRID"
							:data-macro="row.name"
						>
							<div role="cell" class="min-w-0">
								<button
									type="button"
									class="jv-macro-admin-open max-w-full truncate rounded text-left text-sm font-medium text-ink-gray-9 underline-offset-2 hover:underline focus-visible:underline"
									:title="row.macro_name || row.name"
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
										:label="enabledLabel(row.enabled)"
									/>
									<span
										v-if="row.skip_confirmation"
										class="jv-macro-admin-armed inline-flex"
										:title="ARMED_HELP"
									>
										<Badge variant="subtle" theme="orange" label="Armed" />
									</span>
									<span
										v-if="row.admin_hold"
										class="jv-macro-admin-held inline-flex"
										:title="holdTitle(row)"
									>
										<Badge variant="subtle" theme="red" label="On hold" />
										<!-- The title shows on hover only: the reason is in the text too. -->
										<span class="sr-only">{{ holdTitle(row) }}</span>
									</span>
								</div>
							</div>

							<div role="cell" class="min-w-0">
								<div
									class="truncate text-sm text-ink-gray-8"
									:title="row.owner_full_name || row.owner"
								>
									{{ row.owner_full_name || row.owner }}
								</div>
								<div
									v-if="row.owner_full_name && row.owner_full_name !== row.owner"
									class="truncate text-xs text-ink-gray-5"
									:title="row.owner"
								>
									{{ row.owner }}
								</div>
							</div>

							<div role="cell" class="min-w-0">
								<template v-if="row.schedule_enabled">
									<div
										class="jv-macro-admin-schedule truncate text-sm text-ink-gray-8"
										:title="scheduleLabel(row)"
									>
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

							<div role="cell" class="jv-macro-admin-last min-w-0">
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

							<div role="cell" class="flex flex-col items-end gap-1">
								<!-- The run Stop acts on is not always the last one: an older
								     run can still be going after a newer one has ended, and
								     the cell beside this one then reads "Failed". -->
								<span
									v-if="row.live_run && !isLiveRun(row.last_run)"
									class="jv-macro-admin-running text-xs text-ink-blue-3"
									title="An earlier run of this macro is still running"
								>
									Running
								</span>
								<Button
									v-if="row.live_run"
									class="jv-macro-admin-stop"
									size="sm"
									variant="subtle"
									theme="red"
									label="Stop run"
									:loading="stopping === row.name"
									:disabled="busy"
									:aria-label="`Stop the run of ${
										row.macro_name || row.name
									}, owned by ${ownerText(row)}`"
									@click="stop(row)"
								/>
								<!-- An admin's own macro has no Hold and no Hand over: they
								     switch it off on its form, and another admin hands it over.
								     The server refuses both too. Said, so the missing buttons
								     do not read as a fault. -->
								<p
									v-if="isMine(row)"
									class="jv-macro-admin-own text-right text-xs text-ink-gray-5"
								>
									You cannot hold, release or hand over your own macro.
								</p>
								<Button
									v-if="!row.admin_hold && !isMine(row)"
									class="jv-macro-admin-hold"
									size="sm"
									variant="subtle"
									label="Hold"
									:disabled="busy"
									:aria-label="`Put ${
										row.macro_name || row.name
									}, owned by ${ownerText(row)}, on hold`"
									@click="askHold(row)"
								/>
								<Button
									v-if="row.admin_hold && !isMine(row)"
									class="jv-macro-admin-release"
									size="sm"
									variant="subtle"
									label="Release"
									:loading="acting === `release:${row.name}`"
									:disabled="busy"
									:aria-label="`Release the hold on ${
										row.macro_name || row.name
									}, owned by ${ownerText(row)}`"
									@click="release(row)"
								/>
								<Button
									v-if="!isMine(row)"
									class="jv-macro-admin-handover"
									size="sm"
									variant="subtle"
									label="Hand over"
									:disabled="busy"
									:aria-label="`Hand ${
										row.macro_name || row.name
									}, owned by ${ownerText(row)}, to another user`"
									@click="askHandover(row)"
								/>
								<Button
									class="jv-macro-admin-delete"
									size="sm"
									variant="ghost"
									theme="red"
									label="Delete"
									:loading="acting === `delete:${row.name}`"
									:disabled="busy"
									:aria-label="`Delete ${
										row.macro_name || row.name
									}, owned by ${ownerText(row)}`"
									@click="remove(row)"
								/>
							</div>
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
						@click="load('more')"
					/>
				</div>
			</template>
		</div>

		<!-- The macro, to look at: a second dialog over Settings, not a route. Escape
		     closes it alone and focus goes back to the row's button. -->
		<MacroAdminDialog v-if="opened" v-model="detailOpen" :name="opened" @gone="onGone" />
		<MacroHoldDialog
			v-if="holding"
			v-model="holdOpen"
			:name="holding.name"
			:macroName="holding.macro_name || ''"
			:ownerLabel="ownerText(holding)"
			@held="onHeld"
			@failed="onHeld"
		/>
		<MacroHandoverDialog
			v-if="handingOver"
			v-model="handoverOpen"
			:name="handingOver.name"
			:macroName="handingOver.macro_name || ''"
			:owner="handingOver.owner"
			:ownerLabel="ownerText(handingOver)"
			@handed="onHeld"
			@failed="onHeld"
		/>
	</SettingsPane>
</template>

<script setup>
// A Jarvis Admin's view of every user's macros: who owns each, whether it is on,
// scheduled, armed or on hold, how its last run went. Actions: Stop run, Hold
// (MacroHoldDialog asks why), Release, Hand over (MacroHandoverDialog picks who
// to), Delete. Opening a row shows the macro
// read-only (MacroAdminDialog). Nothing here edits or runs a macro.
//
// Gated at the SettingsDialog level by window.is_jarvis_admin. The server checks
// the Jarvis Admin role itself on every call (jarvis.chat.macros_admin_api), so a
// stale client gate can only hide the nav item.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { Badge, Button, FeatherIcon, FormControl, toast } from "frappe-ui";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import MacroAdminDialog from "@/components/settings/MacroAdminDialog.vue";
import MacroHoldDialog from "@/components/settings/MacroHoldDialog.vue";
import MacroHandoverDialog from "@/components/settings/MacroHandoverDialog.vue";
import { session } from "@/data/session";
import { useConfirm } from "@/composables/useConfirm";
import {
	adminDelete,
	adminListMacros,
	adminMacroOwners,
	adminRelease,
	adminStopRun,
} from "@/api/macrosAdmin";
import { errMessage, errHtml, escapeHtml } from "@/lib/errors";
import { isLiveRun, lastRunCell } from "@/lib/macroRunOutcome";
import { nextRunCell } from "@/lib/macroSchedule";
import { scheduleLabel } from "@/pages/macros/scheduleLabel";
import { ARMED_HELP, LAST_RUN_TONE, NEXT_RUN_TONE, enabledLabel } from "@/pages/macros/runDisplay";
import { timeAgo, exactDate, toLocalMs } from "@/utils/datetime";

const PAGE = 20;
// The server returns at most 100 rows a page.
const PAGE_MAX = 100;
// A re-read of what is on screen fetches this many pages at most. Past that the
// list falls back to its first 500 rows, and Load more continues from there.
const KEEP_MAX_PAGES = 5;
const SEARCH_WAIT_MS = 300;
const GRID = "grid-template-columns: 2fr 1.4fr 1.6fr 1fr 6rem";
// Below this the columns would be a few characters of ellipsis each: the list
// scrolls sideways inside its own box instead.
const GRID_MIN = "min-width: 40rem";

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
const HOLD_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "On hold", value: "1" },
	{ label: "Not on hold", value: "0" },
];

const { confirm } = useConfirm();

const rows = ref([]);
const total = ref(0);
const hasMore = ref(false);
const owners = ref([]);
const moreOwners = ref(false);
const loading = ref(false);
const loaded = ref(false);
const error = ref("");
const listEl = ref(null);

const search = ref("");
const filters = reactive({ owner: "", armed: "", scheduled: "", live_run: "", on_hold: "" });
const filtering = computed(
	() => !!search.value.trim() || Object.values(filters).some((v) => v !== "")
);
const ownerOptions = computed(() => [
	{ label: "All owners", value: "" },
	...owners.value.map((o) => ({
		label: `${o.full_name || o.user} (${o.macros})`,
		value: o.user,
	})),
	// The server lists the owners with the most macros, up to a cap.
	...(moreOwners.value
		? [{ label: "More owners: use Search", value: "__more", disabled: true }]
		: []),
]);
const armedAndScheduled = computed(() => filters.armed === "1" && filters.scheduled === "1");

// ── data ─────────────────────────────────────────────────────────────────────
// Three kinds of load, as the Macros Runs tab has (RunsTab.vue, #1597):
//   "reset"  page 1, replaces the list (first load, a filter or search change,
//            Try again with nothing on screen)
//   "more"   the next page, appended (Load more)
//   "keep"   every page already on screen again, replacing the list (Refresh,
//            after a stop, after a macro turned out to be deleted)
// Only the newest request may write to the list: a slow answer to an earlier
// filter must not replace the rows of the one chosen after it.
//
// The rows on screen were always fetched under ONE search and set of filters
// (`loadedUnder`), and are only ever joined with rows fetched under the same.
// A filter change that fails therefore clears them: they belong to the filter
// that was left, and Load more would otherwise append the new filter's second
// page to the old filter's rows.
let latest = 0;
let loadedUnder = "";

function query() {
	const active = {};
	for (const [k, v] of Object.entries(filters)) if (v !== "") active[k] = v;
	return { search: search.value.trim(), filters: active };
}

// Every row on screen again, in pages of PAGE_MAX, one request after the other,
// all under `q`. A "keep" used to ask for one page of at most 100, so a Refresh
// or a stop cut a longer list back to its first 100 rows. Returns null when a
// newer load took over part-way.
async function fetchLoadedPages(q, mine) {
	const want = Math.min(Math.max(rows.value.length, PAGE), PAGE_MAX * KEEP_MAX_PAGES);
	const out = [];
	const seen = new Set();
	let res = {};
	let start = 0;
	while (start < want) {
		const pageLength = Math.min(PAGE_MAX, want - start);
		res = (await adminListMacros({ ...q, start, pageLength })) || {};
		if (mine !== latest) return null;
		// A macro made between two requests pushes a row from the end of one page
		// to the top of the next: list it once.
		for (const r of res.rows || []) {
			if (!seen.has(r.name)) {
				seen.add(r.name);
				out.push(r);
			}
		}
		start += pageLength;
		if (!res.has_more) break;
	}
	return { rows: out, total: res.total, has_more: !!res.has_more };
}

async function load(mode = "reset") {
	const q = query();
	const under = JSON.stringify(q);
	// The search box is typed in and the rows are still the old search's (the wait
	// before it searches): there is no "next page" or "same rows again" of those.
	if (mode !== "reset" && under !== loadedUnder) mode = "reset";
	const mine = ++latest;
	loading.value = true;
	try {
		const res =
			mode === "keep"
				? await fetchLoadedPages(q, mine)
				: (await adminListMacros({
						...q,
						start: mode === "more" ? rows.value.length : 0,
						pageLength: PAGE,
				  })) || {};
		if (mine !== latest || !res) return;
		const page = res.rows || [];
		if (mode === "more") {
			// A macro made or deleted between two pages shifts the rows: never list
			// one twice.
			const seen = new Set(rows.value.map((r) => r.name));
			rows.value = [...rows.value, ...page.filter((r) => !seen.has(r.name))];
		} else {
			rows.value = page;
		}
		total.value = Number(res.total) || 0;
		hasMore.value = !!res.has_more;
		loadedUnder = under;
		error.value = "";
		loaded.value = true;
	} catch (e) {
		if (mine !== latest) return;
		error.value = errMessage(e, "Could not load the macros.");
		loaded.value = true;
		// A refresh that fails keeps the last-good rows. A FILTER CHANGE that fails
		// does not: the rows on screen belong to the filter that was left.
		if (under !== loadedUnder) {
			rows.value = [];
			total.value = 0;
			hasMore.value = false;
		}
	} finally {
		if (mine === latest) loading.value = false;
	}
}

// The owner filter's choices: asked for when the pane opens and on Refresh, not
// with every page or keystroke. Without them the select offers "All owners" only
// and the search box still finds an owner by name, so a failure says nothing.
async function loadOwners() {
	try {
		const res = (await adminMacroOwners()) || {};
		if (Array.isArray(res.owners)) owners.value = res.owners;
		moreOwners.value = !!res.more;
	} catch (e) {
		// keep the choices it had
	}
}

function refresh() {
	loadOwners();
	return load("keep");
}

function setFilter(key, value) {
	filters[key] = value == null ? "" : String(value);
	load();
}

// One step to "armed and scheduled"; pressed again, back to both unset.
function toggleArmedAndScheduled() {
	const on = !armedAndScheduled.value;
	filters.armed = on ? "1" : "";
	filters.scheduled = on ? "1" : "";
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
// The list carries no reason text (it can quote a step). The one thing it says is
// whether an administrator stopped the run, and that is said in the cell itself.
const STOPPED_BY_ADMIN = { text: "Stopped by an admin", reason: "Stopped by an administrator." };

function lastRun(row) {
	const last = row.last_run || null;
	const at = last && (last.finished_at || last.started_at);
	const byAdmin = !!(last && last.stopped_by_admin);
	const cell = lastRunCell({
		// `null`, never undefined: undefined means "an older server", which this
		// endpoint cannot be.
		lastRun: byAdmin ? { ...last, error: STOPPED_BY_ADMIN.reason } : last,
		when: at ? exactDate(at) : "",
		relative: at ? timeAgo(at) : "",
	});
	if (!cell) return cell;
	return {
		...cell,
		text: byAdmin ? STOPPED_BY_ADMIN.text : cell.text,
		class: LAST_RUN_TONE[cell.tone] || "",
	};
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
// The dialog found the macro deleted (the list was loaded before its owner
// deleted it): the row should go too.
function onGone() {
	load("keep");
}

// ── stop a run ───────────────────────────────────────────────────────────────
// The name of the row whose stop is in flight. One at a time: every Stop button
// is disabled meanwhile, and nothing is shown as stopped until the server says so.
const stopping = ref("");

// After a stop the button that had focus is gone (the row has no live run any
// more): put focus on the row's own button, or on the list when the row went too,
// so a keyboard user is not dropped at the top of the dialog.
async function focusRow(name) {
	await nextTick();
	const list = listEl.value;
	if (!list) return;
	const row = [...list.querySelectorAll(".jv-macro-admin-row")].find(
		(el) => el.dataset.macro === name
	);
	const target = (row && row.querySelector(".jv-macro-admin-open")) || list;
	target.focus();
}

async function stop(row) {
	if (busy.value || !row.live_run) return;
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
	} catch (e) {
		toast.error(errHtml(e));
	}
	// Re-read whatever happened. A stop that failed ("This run no longer exists.")
	// means the row is stale, and its Stop button would fail the same way again.
	try {
		await load("keep");
		await focusRow(row.name);
	} finally {
		stopping.value = "";
	}
}

// ── hold, release, hand over, delete ─────────────────────────────────────────
// One action at a time across the list, as for Stop: `acting` is "<verb>:<row>"
// while a release or a delete is in flight, the hold dialog is open for
// `holding` and the hand-over dialog for `handingOver`. Nothing is shown changed until the server says so: each ends with a
// re-read of the list.
const acting = ref("");
const holding = ref(null);
const holdOpen = ref(false);
const handingOver = ref(null);
const handoverOpen = ref(false);
const busy = computed(
	() => !!stopping.value || !!acting.value || holdOpen.value || handoverOpen.value
);

const isMine = (row) => !!session.user && row.owner === session.user;

function holdTitle(row) {
	const reason = (row.admin_hold_reason || "").trim();
	return reason ? `On hold: ${reason}` : "On hold";
}

function askHold(row) {
	if (busy.value) return;
	holding.value = row;
	holdOpen.value = true;
}

function askHandover(row) {
	if (busy.value) return;
	handingOver.value = row;
	handoverOpen.value = true;
}

// After a hold or a hand-over, answered or refused: the row's state on the server
// may have changed either way. A hand-over dialog still open (refused) is given the
// row as re-read, so its owner is the one the server now has: a macro that changed
// hands would otherwise be refused at every retry. One no longer listed closes it,
// its message in a toast.
async function onHeld({ name, message }) {
	await load("keep");
	if (handoverOpen.value && handingOver.value && handingOver.value.name === name) {
		const fresh = rows.value.find((r) => r.name === name);
		if (fresh) {
			handingOver.value = fresh;
		} else {
			handoverOpen.value = false;
			if (message) toast.error(escapeHtml(message));
		}
	}
	await focusRow(name);
}

async function release(row) {
	if (busy.value) return;
	const name = row.macro_name || row.name;
	const ok = await confirm({
		title: "Release the hold?",
		message:
			`Release the hold on “${name}”, owned by ${ownerText(row)}? ` +
			"Nothing turns itself back on: the macro stays off, unscheduled and disarmed until its owner switches it on.",
		confirmLabel: "Release",
	});
	if (!ok) return;
	await act(`release:${row.name}`, row.name, async () => {
		const res = (await adminRelease(row.name)) || {};
		if (res.released === false) {
			toast.create({ message: "This macro was not on hold.", type: "info" });
		} else {
			toast.success(`Released the hold on “${escapeHtml(name)}”`);
		}
	});
}

async function remove(row) {
	if (busy.value) return;
	const name = row.macro_name || row.name;
	const ok = await confirm({
		title: "Delete this macro?",
		message:
			`Delete “${name}”, owned by ${ownerText(row)}? ` +
			(row.live_run ? "Its run is stopped first. " : "") +
			"The macro and its run history go; the chats its runs left stay. " +
			"The owner is told an admin deleted it. This can't be undone.",
		confirmLabel: "Delete",
		danger: true,
	});
	if (!ok) return;
	await act(`delete:${row.name}`, row.name, async () => {
		const res = (await adminDelete(row.name)) || {};
		const stopped = Number(res.stopped_runs) || 0;
		toast.success(
			`Deleted “${escapeHtml(name)}”` +
				(stopped ? `; ${stopped} run${stopped === 1 ? "" : "s"} stopped first` : "")
		);
	});
}

// Runs one action, says what went wrong, and re-reads the list whatever happened:
// a refusal ("This macro was deleted.") means the row is stale.
async function act(key, name, fn) {
	acting.value = key;
	try {
		await fn();
	} catch (e) {
		toast.error(errHtml(e));
	}
	try {
		await load("keep");
		await focusRow(name);
	} finally {
		acting.value = "";
	}
}

onMounted(() => {
	load();
	loadOwners();
});
onBeforeUnmount(() => {
	clearTimeout(searchTimer);
	latest++; // an answer still on its way is nobody's now
});
</script>
