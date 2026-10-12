<template>
	<div
		ref="rootEl"
		class="flex h-full flex-col overflow-hidden"
		@keydown.capture="usedKeyboard(true)"
	>
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: 'Macros', route: { name: 'MacrosList' } }]" />
			</template>
		</LayoutHeader>

		<!-- The body scrolls as one block and the column lives inside it, so the
		     list box keeps its height however short the window is. -->
		<div class="jv-scroll-overlay min-h-0 flex-1 overflow-y-auto px-5 py-4">
			<div class="flex flex-col gap-4">
				<!-- One quiet line saying what this tab is, then two short rows of
				     controls: search with Refresh at the far end, and the filters. The
				     filters carry no label above them: each one's first option says what
				     it filters ("All run states"), and each has its name for a screen reader.
				     Each sits in a slot of fixed width and fills it, so a long owner's
				     name cannot stretch its control past the tab; on a phone the slots
				     become two columns. -->
				<p class="text-p-sm text-ink-gray-5">
					Everyone's macros on this site. Only a macro's owner can edit or run it.
				</p>

				<div class="jv-macro-admin-toolbar flex flex-col gap-2">
					<div class="flex items-center gap-2">
						<div class="jv-macro-admin-search min-w-0 max-w-md flex-1">
							<FormControl
								type="text"
								aria-label="Search"
								placeholder="Search by macro, owner's name or email"
								:modelValue="search"
								@update:modelValue="onSearch"
							>
								<template #prefix>
									<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
								</template>
							</FormControl>
						</div>
						<Button
							class="jv-macro-admin-refresh ml-auto shrink-0"
							variant="subtle"
							icon="refresh-cw"
							label="Refresh"
							tooltip="Refresh"
							:loading="loading"
							@click="refresh"
						/>
					</div>
					<div
						class="jv-macro-admin-filters grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center"
					>
						<div class="jv-macro-admin-filter min-w-0 sm:w-48">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Owner"
								:options="ownerOptions"
								:modelValue="filters.owner"
								@update:modelValue="(v) => setFilter('owner', v)"
							/>
						</div>
						<div class="jv-macro-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Armed"
								:title="ARMED_HELP"
								:options="ARMED_OPTIONS"
								:modelValue="filters.armed"
								@update:modelValue="(v) => setFilter('armed', v)"
							/>
						</div>
						<div class="jv-macro-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Schedule"
								:options="SCHEDULE_OPTIONS"
								:modelValue="filters.scheduled"
								@update:modelValue="(v) => setFilter('scheduled', v)"
							/>
						</div>
						<div class="jv-macro-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Runs"
								:options="LIVE_OPTIONS"
								:modelValue="filters.live_run"
								@update:modelValue="(v) => setFilter('live_run', v)"
							/>
						</div>
						<div class="jv-macro-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Hold"
								:options="HOLD_OPTIONS"
								:modelValue="filters.on_hold"
								@update:modelValue="(v) => setFilter('on_hold', v)"
							/>
						</div>
						<!-- The combination an admin looks for, in one step: it sets the two
						     selects beside it, so what is filtered stays in plain sight. -->
						<Button
							class="jv-macro-admin-preset min-w-0 sm:shrink-0"
							:variant="armedAndScheduled ? 'solid' : 'subtle'"
							label="Armed and scheduled"
							:aria-pressed="armedAndScheduled ? 'true' : 'false'"
							title="Macros that write without asking for confirmation and run on a schedule"
							@click="toggleArmedAndScheduled"
						/>
						<Button
							v-if="filtering"
							class="jv-macro-admin-clear shrink-0"
							variant="ghost"
							label="Clear"
							@click="clearFilters"
						/>
					</div>
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
						@click="loadKeepingFocus('reset', $event)"
					/>
				</div>

				<p v-else-if="!loaded" class="text-p-base text-ink-gray-6" role="status">
					Loading…
				</p>

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
						v-if="error && !moreFailed"
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
							@click="loadKeepingFocus('keep', $event)"
						/>
					</div>

					<!-- The list never needs sideways scrolling: when its box is too
				     narrow for the five lanes, each macro becomes a card (the styles
				     below, by the box's own width, not the window's). `relative` keeps
				     the screen-reader-only header inside the box: it is absolutely
				     positioned, and without a positioned ancestor here it is laid out
				     against the page and widens it. Focusable, so the keyboard can
				     scroll it. Table roles: a screen reader hears each value with its
				     column. -->
					<div
						ref="listEl"
						class="jv-macro-admin-scroll relative overflow-x-auto rounded transition-opacity focus-visible:outline focus-visible:outline-2 focus-visible:outline-outline-gray-3"
						:class="reloading ? 'opacity-50' : ''"
						tabindex="0"
						role="region"
						aria-label="Macros list"
						:aria-busy="reloading ? 'true' : 'false'"
					>
						<div role="table" aria-label="Macros">
							<div
								role="row"
								class="jv-macro-admin-head border-b pb-2 text-xs font-medium text-ink-gray-5"
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
								class="jv-macro-admin-row border-b py-2.5 last:border-b-0"
								:data-macro="row.name"
							>
								<div role="cell" class="jv-macro-admin-cell-macro min-w-0">
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
											<Tooltip :text="ARMED_HELP">
												<Badge
													variant="subtle"
													theme="orange"
													label="Armed"
												/>
											</Tooltip>
											<span class="sr-only">{{ ARMED_HELP }}</span>
										</span>
										<Badge
											v-if="row.admin_hold"
											class="jv-macro-admin-held"
											variant="subtle"
											theme="red"
											label="On hold"
										/>
										<!-- An admin's own macro has no Hold and no Hand over: they
										     switch it off on its form, and another admin hands it
										     over. The server refuses both too. Marked and said, so
										     the shorter set of actions does not read as a fault. -->
										<span
											v-if="isMine(row)"
											class="jv-macro-admin-own inline-flex"
											:title="OWN_HELP"
										>
											<Badge variant="subtle" theme="gray" label="Yours" />
											<span class="sr-only">{{ OWN_HELP }}</span>
										</span>
									</div>
									<!-- Why it is on hold, in the row: a hover title cannot be
									     read on a phone. Two lines at most; the whole of it is in
									     the read-only view. -->
									<p
										v-if="row.admin_hold && holdReason(row)"
										class="jv-macro-admin-hold-reason mt-1 line-clamp-2 break-words text-xs text-ink-gray-6"
									>
										Held: {{ holdReason(row) }}
									</p>
								</div>

								<div role="cell" class="jv-macro-admin-cell-owner min-w-0">
									<div
										class="truncate text-sm text-ink-gray-8"
										:title="row.owner_full_name || row.owner"
									>
										{{ row.owner_full_name || row.owner }}
									</div>
									<div
										v-if="
											row.owner_full_name &&
											row.owner_full_name !== row.owner
										"
										class="truncate text-xs text-ink-gray-5"
										:title="row.owner"
									>
										{{ row.owner }}
									</div>
								</div>

								<div role="cell" class="jv-macro-admin-cell-schedule min-w-0">
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
									<span v-else class="text-sm text-ink-gray-5"
										>Not scheduled</span
									>
								</div>

								<div role="cell" class="jv-macro-admin-cell-last min-w-0">
									<div class="jv-macro-admin-last min-w-0">
										<div
											v-if="lastRun(row)"
											class="truncate text-sm"
											:class="lastRun(row).class || 'text-ink-gray-8'"
											:title="lastRun(row).hint"
										>
											{{ lastRun(row).text }}
										</div>
										<span v-else class="text-sm text-ink-gray-5"
											>Never ran</span
										>
									</div>
									<!-- The run Stop acts on is not always the last one: an older
									     run can still be going after a newer one has ended, and
									     the line above then reads "Failed". -->
									<span
										v-if="row.live_run && !isLiveRun(row.last_run)"
										class="jv-macro-admin-running mt-0.5 flex"
										title="An earlier run of this macro is still running"
									>
										<Badge variant="subtle" theme="blue" label="Running" />
									</span>
								</div>

								<!-- Every action is a button on the row, in one fixed order so
								     the lanes line up down the list: Stop run (only while a run
								     is going), Hold or Release, Hand over, Delete. None is in a
								     menu: this app's menus do not take keyboard focus, and what
								     an admin does to someone's macro has to be reachable by
								     keyboard and stay beside the macro it acts on. Each is named
								     for its row ("Delete Weekly stock check, owned by ..."): the
								     name goes in `label`, which is what the Button reads out, and
								     the short word in the slot is what it shows. -->
								<div role="cell" class="jv-macro-admin-cell-actions">
									<Button
										v-if="row.live_run"
										class="jv-macro-admin-stop"
										size="sm"
										variant="subtle"
										theme="red"
										:label="actionName('Stop run of', row)"
										:loading="stopping === row.name"
										:disabled="busy"
										@click="stop(row)"
										>Stop run</Button
									>
									<Button
										v-if="!row.admin_hold && !isMine(row)"
										class="jv-macro-admin-hold"
										size="sm"
										variant="subtle"
										:label="actionName('Hold', row)"
										:disabled="busy"
										@click="askHold(row)"
										>Hold</Button
									>
									<Button
										v-if="row.admin_hold && !isMine(row)"
										class="jv-macro-admin-release"
										size="sm"
										variant="subtle"
										:label="actionName('Release', row)"
										:loading="acting === `release:${row.name}`"
										:disabled="busy"
										@click="release(row)"
										>Release</Button
									>
									<Button
										v-if="!isMine(row)"
										class="jv-macro-admin-handover"
										size="sm"
										variant="subtle"
										:label="actionName('Hand over', row)"
										:disabled="busy"
										@click="askHandover(row)"
										>Hand over</Button
									>
									<Button
										class="jv-macro-admin-delete"
										size="sm"
										variant="ghost"
										theme="red"
										:label="actionName('Delete', row)"
										:loading="acting === `delete:${row.name}`"
										:disabled="busy"
										@click="remove(row)"
										>Delete</Button
									>
								</div>
							</div>
						</div>
					</div>

					<div class="flex flex-wrap items-center justify-between gap-3">
						<span class="text-xs text-ink-gray-5" aria-live="polite">
							Showing {{ rows.length }} of {{ total }}
						</span>
						<!-- A next page that failed is said here, where the admin is
						     looking: the banner above the list is a screen away. -->
						<div class="flex min-w-0 items-center gap-3">
							<span
								v-if="error && moreFailed"
								class="jv-macro-admin-more-error min-w-0 break-words text-sm text-ink-red-4"
								role="alert"
							>
								{{ error }}
							</span>
							<Button
								v-if="hasMore"
								class="jv-macro-admin-more shrink-0"
								variant="subtle"
								:label="error && moreFailed ? 'Try again' : 'Load more'"
								:loading="loading"
								@click="loadMore"
							/>
						</div>
					</div>
				</template>
			</div>
		</div>

		<!-- The macro, to look at: a dialog over the tab, not a route. Escape closes
		     it and focus goes back to the row's button. -->
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
	</div>
</template>

<script setup>
// A Jarvis Admin's view of every user's macros: who owns each, whether it is on,
// scheduled, armed or on hold, how its last run went. Actions: Stop run, Hold
// (MacroHoldDialog asks why), Release, Hand over (MacroHandoverDialog picks who
// to), Delete. Opening a row shows the macro
// read-only (MacroAdminDialog). Nothing here edits or runs a macro.
//
// The "Admin" tab of the Macros page (/macros/admin), shown by MacrosList only
// when window.is_jarvis_admin. The server checks the Jarvis Admin role itself on
// every call (jarvis.chat.macros_admin_api), so a stale client gate can only hide
// the tab.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { Badge, Breadcrumbs, Button, FeatherIcon, FormControl, Tooltip, toast } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import MacroAdminDialog from "./MacroAdminDialog.vue";
import MacroHoldDialog from "./MacroHoldDialog.vue";
import MacroHandoverDialog from "./MacroHandoverDialog.vue";
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
// Why an admin's own macro shows fewer actions.
const OWN_HELP = "You cannot hold, release or hand over your own macro.";

// The selects have no label above them, so the "everything" option of each says
// what it is all of ("All run states"): five controls side by side that all read
// "All" could not be told apart.
const ARMED_OPTIONS = [
	{ label: "All arming states", value: "" },
	// Not "Armed" alone: with no label above, a chosen value has to say it is a filter.
	{ label: "Armed only", value: "1" },
	{ label: "Not armed", value: "0" },
];
const SCHEDULE_OPTIONS = [
	{ label: "All schedules", value: "" },
	{ label: "Scheduled", value: "1" },
	{ label: "Not scheduled", value: "0" },
];
const LIVE_OPTIONS = [
	{ label: "All run states", value: "" },
	{ label: "Running now", value: "1" },
	{ label: "Not running", value: "0" },
];
const HOLD_OPTIONS = [
	{ label: "All hold states", value: "" },
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
const reloading = ref(false);
const loaded = ref(false);
const error = ref("");
// The load that failed was a next page (Load more): its message goes beside that button.
const moreFailed = ref(false);
const listEl = ref(null);
const rootEl = ref(null);

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
let searchTimer = null;

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
	// Every load carries what is typed in the search box: one still waiting to run
	// would only ask the same again (and take the focus a second time).
	clearTimeout(searchTimer);
	const q = query();
	const under = JSON.stringify(q);
	// The search box is typed in and the rows are still the old search's (the wait
	// before it searches): there is no "next page" or "same rows again" of those.
	if (mode !== "reset" && under !== loadedUnder) mode = "reset";
	const mine = ++latest;
	loading.value = true;
	// The rows on screen are another filter's until this answers: shown as such.
	reloading.value = mode === "reset" && under !== loadedUnder && rows.value.length > 0;
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
		moreFailed.value = false;
		loaded.value = true;
	} catch (e) {
		if (mine !== latest) return;
		error.value = errMessage(e, "Could not load the macros.");
		moreFailed.value = mode === "more";
		loaded.value = true;
		// A refresh that fails keeps the last-good rows. A FILTER CHANGE that fails
		// does not: the rows on screen belong to the filter that was left.
		if (under !== loadedUnder) {
			rows.value = [];
			total.value = 0;
			hasMore.value = false;
		}
	} finally {
		if (mine === latest) {
			loading.value = false;
			reloading.value = false;
		}
	}
}

// The owner filter's choices: asked for when the tab opens and on Refresh, not
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

// A button that is loading is disabled, and the browser takes focus off a disabled
// button: a keyboard user would be dropped at the top of the page. So a load that a
// control here starts goes through `loadKeepingFocus`, which puts focus back where
// it was once the load has answered.
//
// Only for a press made without a pointer. A mouse click focuses a button too, but
// putting focus back after one opens that button's tooltip with nothing to close
// it, and in a text box it raises a phone's keyboard over the list. And never while
// a dialog is open over the tab, or when the user has moved focus themselves.
//
// Which it was is read off the click: one that comes from a key, or from assistive
// technology pressing the button, counts no pointer clicks (`detail` 0). The typed
// search has no click, so it goes by the last thing used in the tab.
//
// A pointer pressed anywhere on the page counts, not only in the tab: the sidebar
// and the page header are outside it, and a click there while a load runs means
// the user has gone elsewhere just the same.
let keyboard = false;
let pointerUses = 0;
function usedKeyboard(yes) {
	keyboard = yes;
	if (!yes) pointerUses += 1;
}
const onPagePointer = () => usedKeyboard(false);
const withoutPointer = (event) =>
	event && typeof event.detail === "number" ? event.detail === 0 : keyboard;
function focusLost() {
	const el = document.activeElement;
	return !el || el === document.body;
}
function dialogOpen() {
	return (
		holdOpen.value ||
		handoverOpen.value ||
		detailOpen.value ||
		!!document.querySelector('[role="alertdialog"]')
	);
}
// `event` is the click that started the load (none for the typed search).
// `after(lost)` picks where focus goes, given the element that had it (which may
// be gone or disabled by now); the default is that element, else the list.
async function loadKeepingFocus(mode, event, after) {
	const at = document.activeElement;
	const mine = withoutPointer(event) && !!rootEl.value && rootEl.value.contains(at);
	const pointerBefore = pointerUses;
	await load(mode);
	// A pointer used while it loaded: the user is somewhere else now.
	if (!mine || pointerUses !== pointerBefore) return;
	await nextTick();
	if (!focusLost() || dialogOpen() || !rootEl.value) return;
	const usable = (el) => !!el && el.isConnected && !el.disabled;
	const target = (after && after(at)) || (usable(at) ? at : null) || listEl.value || searchBox();
	if (usable(target)) target.focus();
}
const searchBox = () => rootEl.value && rootEl.value.querySelector(".jv-macro-admin-search input");

function refresh(event) {
	loadOwners();
	return loadKeepingFocus("keep", event);
}

// The next page. Focus goes to the first row it brought, so the next Tab carries on
// down the list; when it brought none, or failed, it stays on the button.
function loadMore(event) {
	const before = rows.value.length;
	return loadKeepingFocus("more", event, () => {
		const opens = listEl.value ? listEl.value.querySelectorAll(".jv-macro-admin-open") : [];
		return opens.length > before ? opens[before] : null;
	});
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

function onSearch(value) {
	search.value = value == null ? "" : String(value);
	clearTimeout(searchTimer);
	// Tab from the box lands on Refresh, which this load disables for a moment.
	searchTimer = setTimeout(() => loadKeepingFocus("reset"), SEARCH_WAIT_MS);
}

// Clear takes itself away (nothing is filtered any more): for a keyboard user
// focus goes to the search box, the first of the controls it cleared.
function clearFilters(event) {
	search.value = "";
	for (const k of Object.keys(filters)) filters[k] = "";
	return loadKeepingFocus("reset", event, () => searchBox());
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

// A row button's spoken name: the word it shows, then the macro and whose it is.
function actionName(verb, row) {
	return `${verb} ${row.macro_name || row.name}, owned by ${ownerText(row)}`;
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
// so a keyboard user is not dropped at the top of the page.
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

// A stop ends one run. A macro that is on and scheduled starts another at its next time.
function willRunAgain(row) {
	return !!row.enabled && !!row.schedule_enabled && !row.admin_hold;
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
			"The owner will see that an administrator stopped it." +
			(willRunAgain(row)
				? " This stops this run only: the macro stays on its schedule and will run again. To prevent that, put it on hold."
				: ""),
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

// The hold dialog keeps a reason that was typed and not sent, for the macro it was
// typed for. Once that macro is on hold (a hold can be made and still answer an
// error), the reason has served: the closed dialog is taken away, and
// the next one starts empty.
//
// Looked at when the dialog closes and whenever the list is read again, so a hold
// first seen by a later Refresh counts too. Only a row that is listed AND on hold
// ends the draft: one that is merely not on screen (a search, a filter, a later
// page) may be neither held nor gone.
function holdServed() {
	if (holdOpen.value || !holding.value) return "";
	const name = holding.value.name;
	const fresh = rows.value.find((r) => r.name === name);
	if (!fresh || !fresh.admin_hold) return "";
	holding.value = null;
	return name;
}
// The list was read again: the draft goes quietly. Focus is the business of
// whatever started that read.
watch(rows, holdServed);
// The dialog closed. It gave focus back to the Hold button that opened it, which is
// not there any more when the macro is on hold (the row shows Release): focus goes
// to the row instead of being left on the page.
watch(holdOpen, async () => {
	const name = holdServed();
	if (!name) return;
	await nextTick();
	if (focusLost() && !dialogOpen()) await focusRow(name);
});

function holdReason(row) {
	return (row.admin_hold_reason || "").trim();
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
	document.addEventListener("pointerdown", onPagePointer, true);
	load();
	loadOwners();
});
onBeforeUnmount(() => {
	document.removeEventListener("pointerdown", onPagePointer, true);
	clearTimeout(searchTimer);
	latest++; // an answer still on its way is nobody's now
});
</script>

<style scoped>
/* A filter fills its slot and no more: the select's own button would otherwise be
   as wide as its longest option (an owner's full name), and push past the tab. */
.jv-macro-admin-filter :deep([data-slot="trigger"]) {
	width: 100%;
	min-width: 0;
	max-width: 100%;
}
/* The button's width is half of it. Inside, the select keeps an invisible "sizer"
   holding every option's text on one line, in the same grid track as the value:
   left alone the track stays as wide as the longest option, the value never
   truncates, and the text runs out of the button and makes the tab pan sideways.
   Letting the sizer shrink lets the value's own ellipsis work. */
.jv-macro-admin-filter :deep(.select-trigger-sizer) {
	min-width: 0;
	overflow: hidden;
}

/* The list lays itself out by its own width, not the window's: the sidebar may be
   open or closed, and the same tab is 1,180 px wide on a desktop and 335 on a
   phone. */
.jv-macro-admin-scroll {
	container-type: inline-size;
}

/* Wide: five lanes. The last holds every action on one line and is as wide as
   the most a row can show (Stop run, Hold, Hand over, Delete), so the lanes do
   not shift from row to row. */
.jv-macro-admin-head,
.jv-macro-admin-row {
	display: grid;
	grid-template-columns: 2fr 1.4fr 1.6fr minmax(9.5rem, 1.1fr) 19rem;
	align-items: center;
	column-gap: 1rem;
}
.jv-macro-admin-cell-actions {
	display: flex;
	align-items: center;
	justify-content: flex-end;
	gap: 0.25rem;
}

/* Too narrow for five lanes: no sideways scrolling, the row folds. The header row
   stays for a screen reader and is not drawn. First to two lines, the macro and
   its owner over its schedule and last run, with the actions still at the end of
   the row. */
@container (max-width: 60rem) {
	.jv-macro-admin-head {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
		padding: 0;
	}
	.jv-macro-admin-row {
		grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) 19rem;
		grid-template-areas:
			"macro owner actions"
			"schedule last actions";
		align-items: start;
		row-gap: 0.375rem;
	}
	.jv-macro-admin-cell-macro {
		grid-area: macro;
	}
	.jv-macro-admin-cell-owner {
		grid-area: owner;
	}
	.jv-macro-admin-cell-schedule {
		grid-area: schedule;
	}
	.jv-macro-admin-cell-last {
		grid-area: last;
	}
	.jv-macro-admin-cell-actions {
		grid-area: actions;
		align-self: center;
	}
	/* The header row is not drawn here, and "Completed" alone does not say what
	   it is the state of. A screen reader already has the header, so the second
	   declaration gives the caption an empty spoken form where that is supported
	   (generated content is otherwise read out). */
	.jv-macro-admin-cell-last::before {
		content: "Last run";
		content: "Last run" / "";
		display: block;
		font-size: 0.75rem;
		line-height: 1rem;
		color: var(--ink-gray-5, #7c7c7c);
	}
}

/* A phone: a card. The macro, its owner beside its last run, its schedule, then
   its actions on a line of their own, so each is in sight with the macro it acts
   on. */
@container (max-width: 48rem) {
	.jv-macro-admin-row {
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-areas:
			"macro macro"
			"owner last"
			"schedule schedule"
			"actions actions";
		row-gap: 0.5rem;
		padding-top: 0.875rem;
		padding-bottom: 0.875rem;
	}
	.jv-macro-admin-cell-last {
		text-align: right;
	}
	.jv-macro-admin-cell-actions {
		justify-content: flex-start;
		flex-wrap: wrap;
		align-self: start;
	}
}
</style>
