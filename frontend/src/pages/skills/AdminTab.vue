<template>
	<div
		ref="rootEl"
		class="flex h-full flex-col overflow-hidden"
		@keydown.capture="usedKeyboard(true)"
	>
		<!-- As the other tabs of this page: the header is teleported into the app's
		     strip, one tab at a time, so it must name this tab or the strip is empty. -->
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Skills', route: { name: 'SkillsList' } },
						{ label: 'Admin' },
					]"
				/>
			</template>
		</LayoutHeader>

		<!-- The body scrolls as one block and the column lives inside it, so the
		     list box keeps its height however short the window is. -->
		<div class="jv-scroll-overlay min-h-0 flex-1 overflow-y-auto px-5 py-4">
			<div class="flex flex-col gap-4">
				<p class="text-p-sm text-ink-gray-5">
					Everyone's skills on this site. You can read, hold or delete them. Only a
					skill's owner can edit it.
				</p>

				<!-- A change to a skill shared with everyone reaches the assistant only
				     after Apply, so the tab says so until it has been pressed. -->
				<div
					v-if="catalogPending"
					class="jv-skill-admin-catalog flex flex-wrap items-center justify-between gap-3 rounded-md bg-surface-amber-2 px-3 py-2"
					role="status"
				>
					<span class="text-sm text-ink-gray-8">
						Catalog update pending: the shared assistant keeps its old list of skills
						until you press Apply.
					</span>
					<Button
						class="jv-skill-admin-apply"
						size="sm"
						label="Apply"
						:loading="applying || sync.pending"
						@click="apply"
					/>
				</div>

				<p
					v-if="!holdAvailable"
					class="jv-skill-admin-unmigrated text-p-sm text-ink-gray-6"
					role="status"
				>
					Holding is not available yet on this site.
				</p>

				<p
					v-if="!deleteAvailable"
					class="jv-skill-admin-undeletable text-p-sm text-ink-gray-6"
					role="status"
				>
					Deleting is not available yet on this site.
				</p>

				<div class="jv-skill-admin-toolbar flex flex-col gap-2">
					<div class="flex items-center gap-2">
						<div class="jv-skill-admin-search min-w-0 max-w-md flex-1">
							<FormControl
								type="text"
								aria-label="Search"
								placeholder="Search by skill, owner's name or email"
								:modelValue="search"
								@update:modelValue="onSearch"
							>
								<template #prefix>
									<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
								</template>
							</FormControl>
						</div>
						<Button
							class="jv-skill-admin-refresh ml-auto shrink-0"
							variant="subtle"
							icon="refresh-cw"
							label="Refresh"
							tooltip="Refresh"
							:loading="loading"
							@click="refresh"
						/>
					</div>
					<div
						class="jv-skill-admin-filters grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center"
					>
						<div class="jv-skill-admin-filter min-w-0 sm:w-48">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Owner"
								:options="ownerOptions"
								:modelValue="filters.owner"
								@update:modelValue="(v) => setFilter('owner', v)"
							/>
						</div>
						<Button
							class="jv-skill-admin-filters-toggle min-w-0 sm:shrink-0"
							variant="subtle"
							iconRight="chevron-down"
							:label="filtersLabel"
							:aria-expanded="filtersOpen ? 'true' : 'false'"
							aria-controls="jv-skill-admin-more-filters"
							@click="filtersOpen = !filtersOpen"
						/>
						<!-- The combination an admin looks for, in one step: it sets the
						     Audience filter beside it, so what is filtered stays in sight. -->
						<Button
							class="jv-skill-admin-preset col-span-2 min-w-0 sm:col-span-1 sm:shrink-0"
							:variant="companyWrites ? 'solid' : 'subtle'"
							label="Company-wide and writes without asking"
							:aria-pressed="companyWrites ? 'true' : 'false'"
							title="Skills shared with everyone that may write without asking for confirmation"
							@click="toggleCompanyWrites"
						/>
						<Button
							v-if="filtering"
							class="jv-skill-admin-clear shrink-0"
							variant="ghost"
							label="Clear"
							@click="clearFilters"
						/>
					</div>
					<div
						v-if="filtersOpen"
						id="jv-skill-admin-more-filters"
						class="jv-skill-admin-filters grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center"
					>
						<div class="jv-skill-admin-filter min-w-0 sm:w-48">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Audience"
								:options="AUDIENCE_OPTIONS"
								:modelValue="filters.audience"
								@update:modelValue="(v) => setFilter('audience', v)"
							/>
						</div>
						<div class="jv-skill-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="State"
								:options="STATE_OPTIONS"
								:modelValue="filters.enabled"
								@update:modelValue="(v) => setFilter('enabled', v)"
							/>
						</div>
						<div class="jv-skill-admin-filter min-w-0 sm:w-40">
							<FormControl
								class="w-full"
								type="select"
								aria-label="Hold"
								:options="HOLD_OPTIONS"
								:modelValue="filters.on_hold"
								@update:modelValue="(v) => setFilter('on_hold', v)"
							/>
						</div>
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
					<FeatherIcon name="zap" class="size-8 text-ink-gray-4" />
					<span class="text-base text-ink-gray-6">
						{{
							filtering
								? "No skills match these filters."
								: "Nobody has made a skill yet."
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
					     narrow for the five lanes, each skill becomes a card (the styles
					     below, by the box's own width, not the window's). `relative` keeps
					     the screen-reader-only header inside the box. Focusable, so the
					     keyboard can scroll it. Table roles: a screen reader hears each
					     value with its column. -->
					<div
						ref="listEl"
						class="jv-skill-admin-scroll relative overflow-x-auto rounded transition-opacity focus-visible:outline focus-visible:outline-2 focus-visible:outline-outline-gray-3"
						:class="reloading ? 'opacity-50' : ''"
						tabindex="0"
						role="region"
						aria-label="Skills list"
						:aria-busy="reloading ? 'true' : 'false'"
					>
						<div role="table" aria-label="Skills">
							<div
								role="row"
								class="jv-skill-admin-head border-b pb-2 text-xs font-medium text-ink-gray-5"
							>
								<div role="columnheader">Skill</div>
								<div role="columnheader">Owner</div>
								<div role="columnheader">Audience</div>
								<div role="columnheader">Changed</div>
								<div role="columnheader"><span class="sr-only">Actions</span></div>
							</div>

							<div
								v-for="row in rows"
								:key="row.name"
								role="row"
								class="jv-skill-admin-row border-b py-2.5 last:border-b-0"
								:data-skill="row.name"
							>
								<div role="cell" class="jv-skill-admin-cell-skill min-w-0">
									<button
										type="button"
										class="jv-skill-admin-open max-w-full truncate rounded text-left text-sm font-medium text-ink-gray-9 underline-offset-2 hover:underline focus-visible:underline"
										:title="skillLabel(row)"
										:aria-label="`Open ${skillLabel(row)}, owned by ${ownerText(
											row
										)}. Read-only.`"
										@click="open(row)"
									>
										{{ skillLabel(row) }}
									</button>
									<!-- Words, not colour alone, carry each state. -->
									<div class="mt-1 flex flex-wrap items-center gap-1">
										<Badge
											variant="subtle"
											:theme="row.enabled ? 'green' : 'gray'"
											:label="enabledLabel(row.enabled)"
										/>
										<Badge
											class="jv-skill-admin-audience"
											variant="subtle"
											theme="gray"
											:label="audienceLabel(row)"
										/>
										<span
											v-if="row.allow_approve_run"
											class="jv-skill-admin-writes inline-flex"
											:title="WRITES_HELP"
										>
											<Tooltip :text="WRITES_HELP">
												<Badge
													variant="subtle"
													theme="orange"
													label="Writes without asking"
												/>
											</Tooltip>
											<span class="sr-only">{{ WRITES_HELP }}</span>
										</span>
										<Badge
											v-if="row.admin_hold"
											class="jv-skill-admin-held"
											variant="subtle"
											theme="red"
											label="On hold"
										/>
										<Badge
											v-if="row.is_published"
											class="jv-skill-admin-published"
											variant="subtle"
											theme="blue"
											label="Published"
										/>
										<!-- An admin's own skill has no Hold or Release: they switch
										     it off on its form. The server refuses both too. Marked
										     and said, so the shorter set of actions does not read
										     as a fault. -->
										<span
											v-if="row.is_mine"
											class="jv-skill-admin-own inline-flex"
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
										class="jv-skill-admin-hold-reason mt-1 line-clamp-2 break-words text-xs text-ink-gray-6"
									>
										Held: {{ holdReason(row) }}
									</p>
								</div>

								<div role="cell" class="jv-skill-admin-cell-owner min-w-0">
									<div
										class="truncate text-sm text-ink-gray-8"
										:title="ownerName(row)"
									>
										{{ ownerName(row) }}
									</div>
									<div
										v-if="row.owner_name && row.owner_name !== row.owner"
										class="truncate text-xs text-ink-gray-5"
										:title="row.owner"
									>
										{{ row.owner }}
									</div>
								</div>

								<div role="cell" class="jv-skill-admin-cell-audience min-w-0">
									<div
										class="jv-skill-admin-audience-text break-words text-sm text-ink-gray-8"
									>
										{{ audienceDetail(row) }}
									</div>
								</div>

								<div role="cell" class="jv-skill-admin-cell-changed min-w-0">
									<div
										class="truncate text-sm text-ink-gray-8"
										:title="exactDate(row.modified)"
									>
										{{ timeAgo(row.modified) }}
									</div>
								</div>

								<!-- One "Actions" menu, in one fixed order with Delete set apart at
								     the end, so every row ends the same way. It is named for its
								     row: the name goes in `label`, which is what the Button reads
								     out, and the short word in the slot is what it shows. While its
								     menu is open the button is ringed, so it is plain which row
								     the menu belongs to. -->
								<div role="cell" class="jv-skill-admin-cell-actions">
									<Dropdown
										:options="actionsFor(row)"
										:placement="cardLayout ? 'left' : 'right'"
										@update:open="(isOpen) => isOpen && focusOpenMenu()"
									>
										<Button
											class="jv-skill-admin-actions ring-outline-gray-3 data-[state=open]:ring-2"
											size="sm"
											variant="subtle"
											iconRight="chevron-down"
											:label="actionName(row)"
											:loading="acting.endsWith(`:${row.name}`)"
											:disabled="busy"
											>Actions</Button
										>
									</Dropdown>
								</div>
							</div>
						</div>
					</div>

					<div class="flex flex-wrap items-center justify-between gap-3">
						<span class="text-xs text-ink-gray-5" aria-live="polite">
							Showing {{ rows.length }}{{
								total > rows.length ? ` of ${total}` : hasMore ? " so far" : ""
							}}
						</span>
						<!-- A next page that failed is said here, where the admin is
						     looking: the banner above the list is a screen away. -->
						<div class="flex min-w-0 items-center gap-3">
							<span
								v-if="error && moreFailed"
								class="jv-skill-admin-more-error min-w-0 break-words text-sm text-ink-red-4"
								role="alert"
							>
								{{ error }}
							</span>
							<Button
								v-if="hasMore"
								class="jv-skill-admin-more shrink-0"
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

		<!-- The skill, to look at: a dialog over the tab, not a route. Escape closes it
		     and focus goes back to the row's button. -->
		<SkillAdminDialog v-if="opened" v-model="detailOpen" :name="opened" @gone="onGone" />
		<SkillHoldDialog
			v-if="holding"
			v-model="holdOpen"
			:name="holding.name"
			:skillName="skillLabel(holding)"
			:ownerLabel="ownerText(holding)"
			:company="isCompany(holding)"
			@held="onActed"
			@failed="onActed"
		/>
		<SkillReleaseDialog
			v-if="releasing"
			v-model="releaseOpen"
			:name="releasing.name"
			:skillName="skillLabel(releasing)"
			:ownerLabel="ownerText(releasing)"
			:shared="isShared(releasing)"
			@released="onActed"
			@failed="onActed"
		/>
		<!-- Hand over is piece 5b: its dialog and its menu item go here. -->
	</div>
</template>

<script setup>
// A Jarvis Admin's view of every user's skills: who owns each, whether it is on,
// who can use it, whether it writes without asking, whether it is on hold.
// Actions: Hold (SkillHoldDialog asks why), Release (SkillReleaseDialog), Delete.
// Opening a row shows the skill read-only (SkillAdminDialog). Nothing here edits a
// skill.
//
// The "Admin" tab of the Skills page (#admin), shown by SkillsPage only when the
// caps probe says `analysis` (the Jarvis Admin and System Manager roles). The
// server checks the role itself on every call (jarvis.chat.skills_admin_api), so a
// stale client gate can only hide the tab.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
	Badge,
	Breadcrumbs,
	Button,
	Dropdown,
	FeatherIcon,
	FormControl,
	Tooltip,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import SkillAdminDialog from "./SkillAdminDialog.vue";
import SkillHoldDialog from "./SkillHoldDialog.vue";
import SkillReleaseDialog from "./SkillReleaseDialog.vue";
import { useConfirm } from "@/composables/useConfirm";
import { focusLost, useAdminList } from "@/composables/useAdminList";
import { useAdminRowMenu } from "@/composables/useAdminRowMenu";
import { adminDelete, adminGetSkill, adminListSkills, adminSkillOwners } from "@/api/skillsAdmin";
import { applyCustomSkills, getCustomSkillsSyncStatus } from "@/api";
import { errHtml, escapeHtml } from "@/lib/errors";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	OWN_HELP,
	WRITES_HELP,
	audienceDetail,
	audienceLabel,
	enabledLabel,
	isCompany,
	isShared,
	macroUse,
	ownerName,
	ownerText,
	skillLabel,
} from "./skillAdminDisplay";

// The list endpoint pages by number, and the server's page holds this many rows.
const PAGE_SIZE = 20;
const KEEP_MAX_PAGES = 25;
const SYNC_POLL_MS = 3000;
const NUMERIC_FILTERS = ["enabled", "armed", "on_hold"];

// The selects have no label above them, so the "everything" option of each says
// what it is all of ("All audiences"): controls side by side that all read "All"
// could not be told apart.
const AUDIENCE_OPTIONS = [
	{ label: "All audiences", value: "" },
	{ label: "Private", value: "User" },
	{ label: "Shared with a role", value: "Role" },
	{ label: "Everyone", value: "Org" },
];
const STATE_OPTIONS = [
	{ label: "All states", value: "" },
	{ label: "On", value: "1" },
	{ label: "Off", value: "0" },
];
const HOLD_OPTIONS = [
	{ label: "All hold states", value: "" },
	{ label: "On hold", value: "1" },
	{ label: "Not on hold", value: "0" },
];

const { confirm } = useConfirm();

const owners = ref([]);
const moreOwners = ref(false);
const listEl = ref(null);
const rootEl = ref(null);
const sync = ref({ pending: false, needs_apply: false });
// False on a site not yet migrated: Hold and Release are then switched off.
const holdAvailable = ref(true);
const deleteAvailable = ref(true);
// An act just said the catalog needs an Apply (the list may not say it yet).
const needsApply = ref(false);
const applying = ref(false);
const filtersOpen = ref(false);

const search = ref("");
// `armed` has no control of its own: only the "Company-wide and writes without
// asking" button sets it, together with Audience.
const filters = reactive({ owner: "", audience: "", enabled: "", armed: "", on_hold: "" });
const MORE_FILTERS = ["audience", "enabled", "on_hold"];
const filtering = computed(
	() => !!search.value.trim() || Object.values(filters).some((v) => v !== "")
);
const companyWrites = computed(() => filters.audience === "Org" && filters.armed === "1");
const filtersLabel = computed(() => {
	const n = MORE_FILTERS.filter((k) => filters[k] !== "").length;
	return n ? `Filters (${n})` : "Filters";
});
const ownerOptions = computed(() => [
	{ label: "All owners", value: "" },
	...owners.value.map((o) => ({ label: o.full_name || o.user, value: o.user })),
	// The server lists a bounded number of owners.
	...(moreOwners.value
		? [{ label: "More owners: use Search", value: "__more", disabled: true }]
		: []),
]);
const catalogPending = computed(
	() => needsApply.value || !!sync.value.pending || !!sync.value.needs_apply
);

// ── data ─────────────────────────────────────────────────────────────────────
// The load state machine, the focus a reload keeps and the search debounce are
// shared with the Macros tab (composables/useAdminList).
const {
	rows,
	total,
	hasMore,
	loading,
	reloading,
	loaded,
	error,
	moreFailed,
	load,
	loadKeepingFocus,
	loadMore,
	onSearch,
	searchBox,
	usedKeyboard,
} = useAdminList({
	fetchPage,
	onEnvelope: keepEnvelope,
	search,
	filters,
	rootEl,
	listEl,
	dialogOpen,
	searchSelector: ".jv-skill-admin-search",
	openSelector: ".jv-skill-admin-open",
	failureText: "Could not load the skills.",
	pageSize: PAGE_SIZE,
	pageMax: PAGE_SIZE,
	keepMaxPages: KEEP_MAX_PAGES,
});

// The composable asks by offset; the server pages by number, and always in whole
// pages: a reload of 43 rows asks for page 3 and may fill it to its end. `ceil`, so a
// list shortened by a row deleted between two pages moves on instead of asking for
// the same page again.
function fetchPage({ search: text, filters: active, start }) {
	const sent = { ...active };
	for (const key of NUMERIC_FILTERS) if (key in sent) sent[key] = Number(sent[key]);
	return adminListSkills({
		search: text,
		filters: sent,
		page: Math.ceil(start / PAGE_SIZE) + 1,
		pageSize: PAGE_SIZE,
	});
}

// What the list says besides its rows: the catalog's state and whether holding works.
function keepEnvelope(res) {
	sync.value = res.sync || { pending: false, needs_apply: false };
	holdAvailable.value = res.hold_available !== false;
	deleteAvailable.value = res.delete_available !== false;
	if (sync.value.pending) startSyncPoll();
}

// The owner filter's choices: asked for when the tab opens and on Refresh, not
// with every page or keystroke. Without them the select offers "All owners" only
// and the search box still finds an owner by name, so a failure says nothing.
async function loadOwners() {
	try {
		const res = (await adminSkillOwners("")) || {};
		if (Array.isArray(res.owners)) owners.value = res.owners;
		moreOwners.value = !!res.more;
	} catch (e) {
		// keep the choices it had
	}
}

function refresh(event) {
	loadOwners();
	return loadKeepingFocus("keep", event);
}

function setFilter(key, value) {
	filters[key] = value == null ? "" : String(value);
	// The button's second half goes when the audience it belongs to is changed.
	if (key === "audience" && filters.audience !== "Org") filters.armed = "";
	load();
}

// One step to "company-wide and writes without asking"; pressed again, back to both unset.
function toggleCompanyWrites() {
	const on = !companyWrites.value;
	filters.audience = on ? "Org" : "";
	filters.armed = on ? "1" : "";
	if (on) filtersOpen.value = true;
	load();
}

// Clear takes itself away (nothing is filtered any more): for a keyboard user
// focus goes to the search box, the first of the controls it cleared.
function clearFilters(event) {
	search.value = "";
	for (const k of Object.keys(filters)) filters[k] = "";
	return loadKeepingFocus("reset", event, () => searchBox());
}

// ── cells ────────────────────────────────────────────────────────────────────
const holdReason = (row) => (row.admin_hold_reason || "").trim();

// A row button's spoken name: the verb, then the skill and whose it is.
const actionName = (row) => `Actions for ${skillLabel(row)}, owned by ${ownerText(row)}`;

function dialogOpen() {
	return (
		holdOpen.value ||
		releaseOpen.value ||
		detailOpen.value ||
		!!document.querySelector('[role="alertdialog"]')
	);
}

// ── the Actions menu ─────────────────────────────────────────────────────────
// What may be done to a row, in one order: Hold or Release, then Delete in a group
// of its own. An admin's own skill has Delete only (the "Yours" badge says why).
// For a skill shared with everyone every item says so. On a site not yet migrated
// Hold and Release are disabled, not hidden.
function actionsFor(row) {
	const everyone = isCompany(row);
	const item = (key, label, run, extra = {}) => ({
		key,
		label,
		disabled: busy.value,
		onClick: () => fromMenu(row, run),
		...extra,
	});
	const holdOff = { disabled: busy.value || !holdAvailable.value };
	const first = [];
	if (!row.is_mine && !row.is_learned && !row.admin_hold) {
		first.push(item("hold", everyone ? "Hold for everyone" : "Hold", askHold, holdOff));
	}
	if (!row.is_mine && !row.is_learned && row.admin_hold) {
		first.push(item("release", "Release", askRelease, holdOff));
	}
	// Hand over (piece 5b) goes here, between Hold or Release and Delete.
	const groups = [];
	if (first.length) groups.push({ group: "Skill", hideLabel: true, items: first });
	groups.push({
		group: "Delete",
		hideLabel: true,
		items: [
			item("delete", everyone ? "Delete for everyone" : "Delete", remove, {
				theme: "red",
				disabled: busy.value || !!row.is_learned || !deleteAvailable.value,
			}),
		],
	});
	return groups;
}

const { cardLayout, focusOpenMenu, fromMenu, focusRow } = useAdminRowMenu({
	listEl,
	dialogOpen,
	rowSelector: ".jv-skill-admin-row",
	rowKey: "skill",
	actionsSelector: ".jv-skill-admin-actions",
	openSelector: ".jv-skill-admin-open",
});

// ── open one, read-only ──────────────────────────────────────────────────────
const opened = ref("");
const detailOpen = ref(false);
function open(row) {
	opened.value = row.name;
	detailOpen.value = true;
}
// The dialog found the skill deleted (the list was loaded before its owner
// deleted it): the row should go too.
function onGone() {
	load("keep");
}

// ── hold, release, delete ────────────────────────────────────────────────────
// One action at a time across the list: `acting` is "<verb>:<row>" while a delete
// is in flight, and the hold and release dialogs are open for `holding` and
// `releasing`. Nothing is shown changed until the server says so: each ends with a
// re-read of the list.
const acting = ref("");
const holding = ref(null);
const holdOpen = ref(false);
const releasing = ref(null);
const releaseOpen = ref(false);
const busy = computed(() => !!acting.value || holdOpen.value || releaseOpen.value);

// The hold dialog keeps a reason that was typed and not sent, for the skill it was
// typed for. Once that skill is on hold, the reason has served: the closed dialog
// is taken away, and the next one starts empty.
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
watch(rows, holdServed);
// The dialog closed. It gave focus back to the Actions button that opened it, which is
// not there any more when the skill is on hold (the row shows Release): focus goes
// to the row instead of being left on the page.
watch(holdOpen, async () => {
	const name = holdServed();
	if (!name) return;
	await nextTick();
	if (focusLost() && !dialogOpen()) await focusRow(name);
});

function askHold(row) {
	if (busy.value) return;
	holding.value = row;
	holdOpen.value = true;
}

function askRelease(row) {
	if (busy.value) return;
	releasing.value = row;
	releaseOpen.value = true;
}

// After a hold or a release, answered or refused: the row's state on the server
// may have changed either way.
async function onActed({ name, needs_apply }) {
	if (needs_apply) needsApply.value = true;
	await load("keep");
	await focusRow(name);
}

// How many macros use the skill, asked before a delete is confirmed so the admin
// reads it. Null when it could not be read: the row is stale or the call failed,
// and the delete is not offered.
async function macrosUsing(row) {
	try {
		const res = (await adminGetSkill(row.name)) || {};
		return res.used_by_macros || { total: 0, armed: 0 };
	} catch (e) {
		toast.error(errHtml(e));
		await load("keep");
		return null;
	}
}

function deleteMessage(row, used) {
	const name = skillLabel(row);
	const gone = "This can't be undone.";
	if (isCompany(row)) {
		return (
			`Delete “${name}” for everyone? It is shared with everyone on this site, and ` +
			"it goes for all of them. The shared assistant's catalog changes after you press Apply. " +
			`${macroUse(used)} Its owner, ${ownerText(row)}, is told an admin deleted it. ${gone}`
		);
	}
	return (
		`Delete “${name}”, owned by ${ownerText(row)}? ${macroUse(used)} ` +
		`The owner is told an admin deleted it. ${gone}`
	);
}

async function remove(row) {
	if (busy.value) return;
	acting.value = `check:${row.name}`;
	const used = await macrosUsing(row);
	acting.value = "";
	if (!used) return;
	const everyone = isCompany(row);
	const ok = await confirm({
		title: everyone ? "Delete this skill for everyone?" : "Delete this skill?",
		message: deleteMessage(row, used),
		confirmLabel: everyone ? "Delete for everyone" : "Delete",
		danger: true,
	});
	if (!ok) return;
	await act(`delete:${row.name}`, row.name, async () => {
		const res = (await adminDelete(row.name)) || {};
		if (res.needs_apply) needsApply.value = true;
		toast.success(`Deleted “${escapeHtml(skillLabel(row))}”`);
	});
}

// Runs one action, says what went wrong, and re-reads the list whatever happened:
// a refusal ("This skill was deleted.") means the row is stale.
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

// ── apply the catalog ────────────────────────────────────────────────────────
// The same call the Skills list's sync pill makes. It starts the apply and returns;
// the status is polled until it is no longer pending, and the list is read again.
let syncTimer = null;
function startSyncPoll() {
	if (syncTimer) return;
	syncTimer = setInterval(checkSync, SYNC_POLL_MS);
}
function stopSyncPoll() {
	clearInterval(syncTimer);
	syncTimer = null;
}
async function checkSync() {
	try {
		const status = (await getCustomSkillsSyncStatus()) || {};
		if (status.pending) return;
		stopSyncPoll();
		sync.value = { ...sync.value, pending: false };
		await load("keep");
	} catch (e) {
		// best-effort: a transient status failure must not break the tab
	}
}

async function apply() {
	if (applying.value) return;
	applying.value = true;
	try {
		await applyCustomSkills();
		needsApply.value = false;
		sync.value = { ...sync.value, pending: true, needs_apply: false };
		startSyncPoll();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		applying.value = false;
	}
}

onMounted(() => {
	load();
	loadOwners();
});
onBeforeUnmount(stopSyncPoll);
</script>

<style scoped>
/* A filter fills its slot and no more: the select's own button would otherwise be
   as wide as its longest option (an owner's full name), and push past the tab. */
.jv-skill-admin-filter :deep([data-slot="trigger"]) {
	width: 100%;
	min-width: 0;
	max-width: 100%;
}
/* The button's width is half of it. Inside, the select keeps an invisible "sizer"
   holding every option's text on one line, in the same grid track as the value:
   left alone the track stays as wide as the longest option, the value never
   truncates, and the text runs out of the button and makes the tab pan sideways.
   Letting the sizer shrink lets the value's own ellipsis work. */
.jv-skill-admin-filter :deep(.select-trigger-sizer) {
	min-width: 0;
	overflow: hidden;
}

/* The list lays itself out by its own width, not the window's: the sidebar may be
   open or closed, and the same tab is 1,180 px wide on a desktop and 335 on a
   phone. */
.jv-skill-admin-scroll {
	container-type: inline-size;
}

/* Wide: five lanes. The last holds the Actions menu, as wide as it is with its
   spinner, so the lanes do not shift from row to row. */
.jv-skill-admin-head,
.jv-skill-admin-row {
	display: grid;
	grid-template-columns: 2.4fr 1.4fr 1.6fr minmax(7rem, 0.9fr) 8rem;
	align-items: center;
	column-gap: 1rem;
}
.jv-skill-admin-cell-actions {
	display: flex;
	align-items: center;
	justify-content: flex-end;
	gap: 0.25rem;
	/* Room for the 2px ring on the open Actions button: the list box clips at its
	   edge, and the button sits right on it (on its left in a card). */
	padding: 0.25rem;
}

/* Too narrow for five lanes: no sideways scrolling, the row folds. The header row
   stays for a screen reader and is not drawn. First to two lines, the skill and
   its owner over its audience and change time, with the actions still at the end
   of the row. */
@container (max-width: 56rem) {
	.jv-skill-admin-head {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
		padding: 0;
	}
	.jv-skill-admin-row {
		grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) 8rem;
		grid-template-areas:
			"skill owner actions"
			"audience changed actions";
		align-items: start;
		row-gap: 0.375rem;
	}
	.jv-skill-admin-cell-skill {
		grid-area: skill;
	}
	.jv-skill-admin-cell-owner {
		grid-area: owner;
	}
	.jv-skill-admin-cell-audience {
		grid-area: audience;
	}
	.jv-skill-admin-cell-changed {
		grid-area: changed;
	}
	.jv-skill-admin-cell-actions {
		grid-area: actions;
		align-self: center;
	}
	/* The header row is not drawn here, and "2 days ago" alone does not say what
	   it is. A screen reader already has the header, so the second declaration
	   gives the caption an empty spoken form where that is supported (generated
	   content is otherwise read out). */
	.jv-skill-admin-cell-changed::before {
		content: "Changed";
		content: "Changed" / "";
		display: block;
		font-size: 0.75rem;
		line-height: 1rem;
		color: var(--ink-gray-5, #7c7c7c);
	}
}

/* A phone: a card. The skill, its owner beside its change time, its audience, then
   its actions on a line of their own, so each is in sight with the skill it acts on. */
@container (max-width: 48rem) {
	.jv-skill-admin-row {
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-areas:
			"skill skill"
			"owner changed"
			"audience audience"
			"actions actions";
		row-gap: 0.5rem;
		padding-top: 0.875rem;
		padding-bottom: 0.875rem;
	}
	.jv-skill-admin-cell-changed {
		text-align: right;
	}
	.jv-skill-admin-cell-actions {
		justify-content: flex-start;
		flex-wrap: wrap;
		align-self: start;
	}
}
</style>
