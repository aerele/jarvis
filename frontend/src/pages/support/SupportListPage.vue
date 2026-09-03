<template>
	<SupportShell :crumbs="[{ label: 'Support' }]">
		<template #actions>
			<!-- The house puts the primary action in ListPage's #right-header, which
			     renders inside LayoutHeader — but LayoutHeader teleports to
			     #app-header, which does NOT exist on a chromeless route. So the
			     action moves to the support top bar, keeping the same recipe:
			     solid + iconLeft="plus" + "New <Thing>", rightmost. -->
			<Button
				variant="solid"
				label="New ticket"
				iconLeft="plus"
				@click="router.push({ name: 'SupportNew' })"
			/>
		</template>

		<ListPage
			class="min-h-0 flex-1"
			:show-header="false"
			:columns="columns"
			:rows="rows"
			:loading="store.ticketsLoading"
			:error="store.ticketsError"
			:total="filtered.length"
			:quick-filters="quickFilters"
			:filter-defs="filterDefs"
			:filters="filters"
			:sort-options="sortOptions"
			:sort="sort"
			:page-length="pageLength"
			:default-sort="DEFAULT_SORT"
			:get-row-route="getRowRoute"
			storage-key="support"
			:empty-state="emptyState"
			@update:filters="setFilters"
			@update:sort="(s) => (sort = s)"
			@update:page-length="onPageLength"
			@load-more="shown += pageLength"
			@refresh="store.loadTickets()"
		>
			<template #cell-subject="{ row }">
				<div class="flex items-center gap-2 overflow-hidden">
					<Tooltip
						v-if="store.isAwaiting(row.status)"
						text="Support replied - awaiting you"
					>
						<div
							class="size-1.5 shrink-0 rounded-full bg-surface-amber-2"
							aria-hidden="true"
						/>
					</Tooltip>
					<div
						class="truncate text-base font-medium text-ink-gray-9"
						:title="row.subject || row.name"
					>
						{{ row.subject || "(no subject)" }}
					</div>
				</div>
			</template>

			<template #cell-name="{ row }">
				<div class="truncate text-base text-ink-gray-6" :title="`#${row.name}`">
					#{{ row.name }}
				</div>
			</template>

			<template #cell-status="{ row }">
				<Badge
					:variant="badgeFor(row.status).variant"
					:theme="badgeFor(row.status).theme"
					:label="badgeFor(row.status).label"
				/>
			</template>

			<template #cell-priority="{ row }">
				<Badge
					v-if="priorityBadge(row.priority)"
					:variant="priorityBadge(row.priority).variant"
					:theme="priorityBadge(row.priority).theme"
					:label="priorityBadge(row.priority).label"
				/>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-modified="{ row }">
				<div class="flex w-full items-center justify-end">
					<Tooltip v-if="row.modified" :text="exactDate(row.modified)">
						<div class="truncate text-base">{{ timeAgo(row.modified) }}</div>
					</Tooltip>
					<span v-else class="text-base text-ink-gray-4">-</span>
				</div>
			</template>
		</ListPage>
	</SupportShell>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import { Badge, Button, Tooltip } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import SupportShell from "@/components/support/SupportShell.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import { useSupportStore, badgeFor, priorityBadge } from "@/stores/support";

const route = useRoute();
const router = useRouter();
const store = useSupportStore();

// Status lives in the quick-filter strip, NOT in a tab bar. House grammar:
// TabBar is for switching between different VIEWS (Macros|Runs, Skills|Wiki);
// filtering one list by a field is a quick-filter select (MacrosList's Status
// and Schedule, SkillsList's Status). The "awaiting you" signal still reads at
// a glance from the row dot and the user-menu badge.
const STATUS_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Awaiting you", value: "awaiting" },
	{ label: "Open", value: "open" },
	{ label: "Closed", value: "closed" },
];
const KNOWN_STATUS_VALUES = STATUS_OPTIONS.map((o) => o.value).filter(Boolean);

// Deep-link seed (?status=) - same recipe as ApprovalsBoard's initialStatus
// (route.query validated against the known quick-filter values, read once at
// setup rather than watched). The chat header's ticket-count pill routes here
// with ?status=awaiting so the list opens pre-filtered instead of showing
// everything and making the user re-pick the filter they just clicked for.
const initialStatus = KNOWN_STATUS_VALUES.includes(route.query.status) ? route.query.status : "";

const ALL_COLUMNS = [
	{ label: "Subject", key: "subject", width: 3 },
	{ label: "Ticket", key: "name", width: "9rem" },
	{ label: "Status", key: "status", width: "8rem" },
	{ label: "Priority", key: "priority", width: "7rem" },
	{ label: "Updated", key: "modified", width: "8rem", align: "right" },
];

// Below ~640px the fixed 9rem+8rem+8rem of Ticket/Status/Updated overflows a
// phone-width viewport — mirrors the design reference, which hides this same
// column at its own breakpoint. Dropping it from the `columns` prop (rather
// than hiding cells with :deep() CSS against frappe-ui's internal ListView
// grid) is the lever ListPage already exposes for exactly this.
const isNarrow = ref(false);
let narrowMq = null;
function onNarrowChange(e) {
	isNarrow.value = e.matches;
}
onMounted(() => {
	if (typeof window === "undefined" || !window.matchMedia) return;
	narrowMq = window.matchMedia("(max-width: 640px)");
	isNarrow.value = narrowMq.matches;
	narrowMq.addEventListener("change", onNarrowChange);
});
onUnmounted(() => {
	if (narrowMq) narrowMq.removeEventListener("change", onNarrowChange);
});
// Mirrors ColumnsButton's own persisted hide-list (jarvis-cols-<storage-key>,
// storage-key="support" on the ListPage below) read-only — ColumnsButton/
// ListPage own writing it. Needed so the mobile drop below can tell whether
// dropping "name" too would leave zero visible columns: if the user already
// hid Subject/Status/Updated so "Ticket" is the only column left, the mobile
// drop must not remove it as well and render an empty grid.
const userHiddenCols = useStorage("jarvis-cols-support", []);
// Priority is self-activating: the control plane only began returning `priority`
// after a later deploy, so until a row actually carries it we omit the column
// rather than render an all-blank one that reads as broken. It lights up on its
// own the moment the data arrives.
const hasPriority = computed(() => store.tickets.some((t) => t.priority));
// Secondary fixed-width columns shed below ~640px so Ticket+Status+Priority+Updated
// don't overflow a phone. Both `name` and `priority` are in the drop set.
const NARROW_DROP = ["name", "priority"];
const columns = computed(() => {
	const base = ALL_COLUMNS.filter((c) => c.key !== "priority" || hasPriority.value);
	if (!isNarrow.value) return base;
	// Guard against zero visible columns: if the user has already hidden everything
	// except the drop-set columns, keep the full set rather than render an empty grid.
	const survivors = base.filter(
		(c) => !NARROW_DROP.includes(c.key) && !userHiddenCols.value.includes(c.key)
	);
	return survivors.length ? base.filter((c) => !NARROW_DROP.includes(c.key)) : base;
});

// Search rides the quick-filter strip as a text control, exactly as every other
// list page does. Here it filters client-side: list_tickets takes no arguments.
const quickFilters = [
	{ key: "search", label: "Search tickets", type: "text" },
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
];
// Filter popover mirrors the house pattern (Skills/Macros repeat their quick
// filters here plus an extra dimension). Status alone would be a single-def
// filter whose "+ Add Filter" control unmounts the instant you pick it — closing
// the popover, the "vanishing" the user hit; pairing it with the Updated date
// range always leaves a field unset after the first pick, exactly like Skills'
// multi-column filter.
const filterDefs = [
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
	{ key: "updated", label: "Updated", type: "daterange" },
];

const sortOptions = [
	{ label: "Updated", value: "modified" },
	{ label: "Subject", value: "subject" },
	{ label: "Status", value: "status" },
];
const DEFAULT_SORT = { field: "modified", dir: "desc" };

const filters = reactive(initialStatus ? { status: initialStatus } : {});
const sort = ref({ ...DEFAULT_SORT });
const pageLength = ref(20);
const shown = ref(20);

// ListPage emits a WHOLE new filters object, not a patch — mirror that.
function setFilters(next) {
	for (const k of Object.keys(filters)) delete filters[k];
	Object.assign(filters, next || {});
}

function onPageLength(v) {
	pageLength.value = v;
	shown.value = v;
}

function matchesStatus(t) {
	const f = filters.status;
	if (!f) return true;
	if (f === "awaiting") return store.isAwaiting(t.status);
	if (f === "closed") return store.isClosed(t.status);
	// "open" is the catch-all's mirror: anything neither closed nor awaiting.
	return !store.isClosed(t.status) && !store.isAwaiting(t.status);
}

// FilterButton's daterange writes from_date/to_date (not the def key). Compare on
// the naive YYYY-MM-DD prefix of `modified` (site-tz, per helpdesk_client) against
// the picker's YYYY-MM-DD bounds — a lexical compare is correct for that format.
function matchesDate(t) {
	const { from_date: from, to_date: to } = filters;
	if (!from && !to) return true;
	const d = (t.modified || "").slice(0, 10);
	if (!d) return false;
	if (from && d < from) return false;
	if (to && d > to) return false;
	return true;
}

const filtered = computed(() => {
	const q = (filters.search || "").trim().toLowerCase();
	const out = store.tickets.filter(
		(t) =>
			matchesStatus(t) &&
			matchesDate(t) &&
			(!q ||
				(t.subject || "").toLowerCase().includes(q) ||
				(t.name || "").toLowerCase().includes(q))
	);
	const { field, dir } = sort.value;
	const mul = dir === "asc" ? 1 : -1;
	// Status sorts by what the badge actually shows ("Awaiting you" / "Open" /
	// "Closed"), not the raw Helpdesk value (Replied/Resolved/Paused/...) — the
	// groups happen to line up today, but a new status could split two rows the
	// user sees as the same group.
	const sortKey = (t) => (field === "status" ? badgeFor(t.status).label : t[field]);
	return [...out].sort(
		(a, b) => String(sortKey(a) || "").localeCompare(String(sortKey(b) || "")) * mul
	);
});

// Client-side paging so the footer's Load More and the 20/50/100 switcher are
// live controls rather than decoration.
const rows = computed(() => filtered.value.slice(0, shown.value));

// Reset paging only when the FILTER/SORT INPUTS change the meaning of the
// result set — not whenever `filtered` merely recomputes. `filtered` also
// recomputes on every store.tickets reassignment (loadTickets() always
// assigns a fresh array), so watching it directly snapped a user's loaded
// rows back to one page every time they hit Refresh (or a future background
// poll landed) mid-scroll.
watch(
	[
		() => filters.status,
		() => filters.search,
		() => filters.from_date,
		() => filters.to_date,
		sort,
	],
	() => (shown.value = pageLength.value)
);

// list_tickets is capped at the newest 50 (helpdesk_client.py) and search is
// client-side over whatever's loaded — so a zero-match search when exactly 50
// are loaded must not read as "that ticket doesn't exist": an older one may
// simply be outside the window this page ever fetched.
const CAP_NOTE = " Showing your 50 most recent tickets - an older one may not appear here.";

const emptyState = computed(() =>
	filters.search || filters.status
		? {
				icon: "life-buoy",
				title: "No tickets match",
				description:
					"Try a different search or status." +
					(filters.search && store.tickets.length === 50 ? CAP_NOTE : ""),
		  }
		: {
				icon: "life-buoy",
				title: "No tickets yet",
				description:
					"When you ask for help, your conversations with our support team appear here.",
		  }
);

function getRowRoute(row) {
	return { name: "SupportTicket", params: { ticket: row.name } };
}

onMounted(() => {
	store.loadTickets();
	store.refreshAwaiting();
});
</script>
