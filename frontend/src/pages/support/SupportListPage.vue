<template>
	<SupportShell title="Support">
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
						<div class="size-1.5 shrink-0 rounded-full bg-surface-amber-2" />
					</Tooltip>
					<div
						class="truncate text-base font-medium text-ink-gray-9"
						:title="row.subject || row.name"
					>
						{{ row.subject || "(no subject)" }}
					</div>
				</div>
			</template>

			<template #cell-status="{ row }">
				<Badge
					variant="subtle"
					:theme="badgeFor(row.status).theme"
					:label="badgeFor(row.status).label"
				/>
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
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { Badge, Button, Tooltip } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import SupportShell from "@/components/support/SupportShell.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import { useSupportStore, badgeFor } from "@/stores/support";

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

const columns = [
	{ label: "Subject", key: "subject", width: 3 },
	{ label: "Ticket", key: "name", width: "9rem" },
	{ label: "Status", key: "status", width: "8rem" },
	{ label: "Updated", key: "modified", width: "8rem", align: "right" },
];

// Search rides the quick-filter strip as a text control, exactly as every other
// list page does. Here it filters client-side: list_tickets takes no arguments.
const quickFilters = [
	{ key: "search", label: "Search tickets", type: "text" },
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
];
const filterDefs = [{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS }];

const sortOptions = [
	{ label: "Updated", value: "modified" },
	{ label: "Subject", value: "subject" },
	{ label: "Status", value: "status" },
];
const DEFAULT_SORT = { field: "modified", dir: "desc" };

const filters = reactive({});
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

const filtered = computed(() => {
	const q = (filters.search || "").trim().toLowerCase();
	const out = store.tickets.filter(
		(t) =>
			matchesStatus(t) &&
			(!q ||
				(t.subject || "").toLowerCase().includes(q) ||
				(t.name || "").toLowerCase().includes(q))
	);
	const { field, dir } = sort.value;
	const mul = dir === "asc" ? 1 : -1;
	return [...out].sort(
		(a, b) => String(a[field] || "").localeCompare(String(b[field] || "")) * mul
	);
});

// Client-side paging so the footer's Load More and the 20/50/100 switcher are
// live controls rather than decoration.
const rows = computed(() => filtered.value.slice(0, shown.value));

watch(filtered, () => (shown.value = pageLength.value));

const emptyState = computed(() =>
	filters.search || filters.status
		? {
				icon: "life-buoy",
				title: "No tickets match",
				description: "Try a different search or status.",
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
