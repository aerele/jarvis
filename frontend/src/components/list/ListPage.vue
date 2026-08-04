<template>
	<div class="flex h-full flex-col overflow-hidden">
		<!-- showHeader=false: embedded mode (e.g. the app-learning runs list
		     inside AnalysisTab) - the host page already owns the single
		     LayoutHeader teleport to #app-header (SkillsPage's "exactly one at
		     a time" rule), so a second one here would stack a duplicate
		     header strip into the shell. -->
		<LayoutHeader v-if="showHeader">
			<template #left-header>
				<Breadcrumbs :items="breadcrumbs" />
			</template>
			<template #right-header>
				<slot name="right-header" />
			</template>
		</LayoutHeader>

		<!-- toolbar: quick filters | divider | Refresh · Filter · Sort · Columns -->
		<div class="flex items-center justify-between gap-2 px-5 py-4">
			<div class="-ml-1 flex h-9 flex-1 items-center overflow-x-auto">
				<div v-for="qf in quickFilters" :key="qf.key" class="m-1 min-w-36">
					<FormControl
						v-if="qf.type === 'select'"
						type="select"
						:options="qf.options"
						:modelValue="quickValue(qf)"
						@update:modelValue="(v) => applyQuick(qf, v)"
					/>
					<FormControl
						v-else
						type="text"
						:placeholder="qf.label"
						:modelValue="quickValue(qf)"
						@update:modelValue="(v) => onQuickText(qf, v)"
					/>
				</div>
			</div>
			<div class="-ml-2 h-[70%] border-l" />
			<div class="flex items-center gap-2">
				<Button
					:tooltip="'Refresh'"
					icon="refresh-cw"
					:loading="loading"
					@click="$emit('refresh')"
				/>
				<!-- Two filter surfaces, never both. A MIGRATED list (plan 08: it
				     passes `filterState` with a registered view key) gets the shared
				     schema-driven FilterGroup, whose field set is the server's
				     per-caller catalog. Everything else keeps FilterButton and its
				     page-authored `filterDefs` until its own wave migrates.
				     A page with no filter dimensions at all (e.g. Support, where
				     Status is the only facet and it already rides the quick-filter
				     strip) would otherwise render a dead Filter button whose popover
				     only says "Empty - Choose a field to filter by". Mirror
				     ColumnsButton's own v-if and drop it entirely. -->
				<FilterGroup
					v-if="filterState && filterState.viewKey"
					:schema="filterState.schema"
					:schema-state="filterState.schemaState"
					:schema-error="filterState.schemaError"
					:clauses="filterState.clauses"
					:error="filterState.error"
					@update:clauses="(c, o) => $emit('update:filterClauses', c, o)"
					@request-schema="$emit('request-filter-schema')"
				/>
				<FilterButton
					v-else-if="filterDefs.length"
					:filter-defs="filterDefs"
					:filters="filters"
					@update:filters="(f) => $emit('update:filters', f)"
				/>
				<SortButton
					:sort-options="sortOptions"
					:sort="sort"
					:default-sort="defaultSort"
					@update:sort="(s) => $emit('update:sort', s)"
				/>
				<ColumnsButton
					v-if="storageKey"
					:columns="columns"
					:storage-key="storageKey"
					@update:hidden="(keys) => (hiddenKeys = keys)"
				/>
			</div>
		</div>

		<!-- Filter notices ride ABOVE the banner slot and OUTSIDE the popover: a
		     link whose clauses no longer exist, and a rejection the server coded,
		     both have to be readable without opening the panel that caused them
		     (plan §8 step 4, "visibly report dropped/unavailable clauses").
		     They STACK — a rejection does not hide the note explaining that some
		     of the link's filters were removed, because the note is often the
		     reason the results look wrong. -->
		<div v-if="filterNotices.length" class="flex flex-col gap-2 px-5 pb-3">
			<div
				v-for="notice in filterNotices"
				:key="notice.key"
				class="flex items-center justify-between gap-2 rounded-md p-2"
				:class="notice.tone === 'error' ? 'bg-surface-red-2' : 'bg-surface-amber-2'"
				role="status"
			>
				<div class="flex items-center gap-2">
					<FeatherIcon
						:name="notice.tone === 'error' ? 'alert-triangle' : 'info'"
						class="size-4"
						:class="notice.tone === 'error' ? 'text-ink-red-3' : 'text-ink-amber-3'"
					/>
					<span class="text-base font-medium text-ink-gray-8">
						{{ notice.message }}
					</span>
				</div>
				<div class="flex items-center gap-1">
					<Button
						v-if="notice.retry"
						label="Retry"
						:loading="loading"
						@click="$emit('refresh')"
					/>
					<Button
						v-if="notice.dismissible"
						variant="ghost"
						icon="x"
						aria-label="Dismiss"
						@click="$emit('dismiss-filter-notice')"
					/>
				</div>
			</div>
		</div>

		<div v-if="$slots.banner" class="px-5">
			<slot name="banner" />
		</div>

		<!-- list body -->
		<!-- !w-full: upstream's inner wrapper is `w-max min-w-full`, which sizes
		     the grid to max-content - long cells push trailing columns past the
		     viewport behind a subtle inner scrollbar. Pinning to the container
		     lets the minmax(0, Nfr) tracks compress and truncation work. -->
		<ListView
			v-if="rows.length"
			class="!w-full"
			:columns="visibleColumns"
			:rows="rows"
			:row-key="rowKey"
			:options="{
				selectable,
				getRowRoute: getRowRoute || null,
				onRowClick: onRowClick || null,
				rowHeight: 40,
				resizeColumn: false,
				showTooltip: true,
			}"
			@update:selections="(s) => $emit('update:selections', s)"
		>
			<template #default>
				<ListHeader class="sm:mx-5 mx-3">
					<ListHeaderItem
						v-for="column in visibleColumns"
						:key="column.key"
						:item="column"
					/>
				</ListHeader>
				<ListRows class="mx-3 sm:mx-5" />
				<ListSelectBanner v-if="selectable">
					<template #actions="{ selections, unselectAll }">
						<slot name="select-actions" v-bind="{ selections, unselectAll }" />
					</template>
				</ListSelectBanner>
			</template>
			<!-- DA-08: cell renderers dispatch through ListView's named `cell` slot
			     ({column, row, item, align}) and forward to #cell-<key> ({row, column, item});
			     no page slot for a column ⇒ stock ListRowItem. -->
			<template #cell="{ column, row, item, align }">
				<slot :name="`cell-${column.key}`" v-bind="{ row, column, item }">
					<ListRowItem :column="column" :row="row" :item="item" :align="align" />
				</slot>
			</template>
		</ListView>

		<!-- error state (fetch failed, no rows to show) - precedes the empty state
		     so a broken fetch isn't misreported as "no records" -->
		<div v-else-if="error" class="relative flex-1">
			<div
				class="absolute left-1/2 flex w-4/12 -translate-x-1/2 flex-col items-center gap-3"
				:style="{ top: '35%' }"
			>
				<FeatherIcon name="alert-circle" class="size-7.5 text-ink-red-4" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8"
						>Couldn't load this list</span
					>
					<span class="text-center text-p-base text-ink-red-4">{{ error }}</span>
				</div>
			</div>
		</div>

		<!-- empty state (loaded, zero rows) -->
		<div v-else-if="!loading" class="relative flex-1">
			<div
				class="absolute left-1/2 flex w-4/12 -translate-x-1/2 flex-col items-center gap-3"
				:style="{ top: '35%' }"
			>
				<FeatherIcon
					:name="(emptyState && emptyState.icon) || 'file-text'"
					class="size-7.5 text-ink-gray-5"
				/>
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8">
						{{ (emptyState && emptyState.title) || "No records found" }}
					</span>
					<span
						v-if="emptyState && emptyState.description"
						class="text-center text-p-base text-ink-gray-6"
					>
						{{ emptyState.description }}
					</span>
				</div>
			</div>
		</div>
		<div v-else class="flex-1" />

		<ListFooter
			v-if="rows.length"
			class="border-t sm:px-5 px-3 py-2"
			:modelValue="pageLength"
			:options="{ rowCount: rows.length, totalCount: total }"
			@update:modelValue="(v) => $emit('update:pageLength', v)"
			@loadMore="$emit('loadMore')"
		>
			<!-- own the right side: upstream ListFooter's Load More <Button> is a
			     bare unregistered element in frappe-ui 0.1.278 (Button never
			     imported), so paging beyond page 1 is otherwise unreachable -->
			<template #right>
				<div class="flex items-center">
					<Button
						v-if="rows.length < total"
						label="Load More"
						@click="$emit('loadMore')"
					/>
					<div v-if="rows.length < total" class="mx-3 h-[80%] border-l" />
					<div class="flex items-center gap-1 text-base text-ink-gray-5">
						<div>{{ rows.length }}</div>
						<div>of</div>
						<div>{{ total }}</div>
					</div>
				</div>
			</template>
		</ListFooter>
	</div>
</template>

<script setup>
// ListPage - the standard list frame (DESIGN-V3 §5.2 + §14 F2/DA-08):
// LayoutHeader breadcrumbs → toolbar (quick filters · Refresh/Filter/Sort/Columns)
// → banner slot → frappe-ui ListView composition → ListFooter load-more.
import { ref, computed, onBeforeUnmount } from "vue";
import {
	ListView,
	ListHeader,
	ListHeaderItem,
	ListRows,
	ListRowItem,
	ListSelectBanner,
	ListFooter,
	Breadcrumbs,
	Button,
	FormControl,
	FeatherIcon,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import FilterButton from "@/components/list/FilterButton.vue";
import FilterGroup from "@/components/list/FilterGroup.vue";
import SortButton from "@/components/list/SortButton.vue";
import ColumnsButton from "@/components/list/ColumnsButton.vue";

const props = defineProps({
	breadcrumbs: { type: Array, default: () => [] }, // [{label, route?}]
	showHeader: { type: Boolean, default: true }, // false = embedded (host owns the LayoutHeader)
	columns: { type: Array, default: () => [] }, // frappe-ui ListView columns
	rows: { type: Array, default: () => [] },
	rowKey: { type: String, default: "name" },
	loading: { type: Boolean, default: false },
	error: { type: String, default: "" }, // fetch-failure message; shows the error state instead of the empty state
	total: { type: Number, default: 0 },
	hasMore: { type: Boolean, default: false },
	quickFilters: { type: Array, default: () => [] }, // [{key,label,type,options}]
	filterDefs: { type: Array, default: () => [] }, // [{key,label,type:'select'|'daterange',options}]
	filters: { type: Object, default: () => ({}) },
	// plan 08: useListPage's `filterState` for a MIGRATED surface. Null (the
	// default) keeps the legacy FilterButton path for every unmigrated list.
	filterState: { type: Object, default: null },
	sortOptions: { type: Array, default: () => [] }, // [{label, value}]
	sort: { type: Object, default: () => ({ field: "", dir: "" }) },
	pageLength: { type: Number, default: 20 },
	defaultSort: { type: Object, default: () => ({ field: "", dir: "" }) },
	selectable: { type: Boolean, default: false },
	getRowRoute: { type: Function, default: null },
	onRowClick: { type: Function, default: null },
	emptyState: { type: Object, default: () => ({}) }, // {title, description, icon?}
	storageKey: { type: String, default: "" }, // §14 F2 - column show/hide persistence
});

const emit = defineEmits([
	"update:filters",
	"update:filterClauses",
	"request-filter-schema",
	"dismiss-filter-notice",
	"update:sort",
	"update:pageLength",
	"loadMore",
	"refresh",
	"update:selections",
]);

// Two sources, stacked, note first. The note is dismissible (the user has read
// it and the clauses are already gone); a coded rejection is not, because it is
// still true — it clears when a request succeeds. `transient` (our schema or
// service, not the user's filter) is the only one worth a Retry.
const filterNotices = computed(() => {
	const state = props.filterState;
	if (!state) return [];
	const notices = [];
	if (state.notice) {
		notices.push({
			key: "notice",
			message: state.notice,
			tone: "warning",
			retry: false,
			dismissible: true,
		});
	}
	if (state.error) {
		notices.push({
			key: "error",
			message: state.error.message,
			tone: state.error.kind === "transient" ? "warning" : "error",
			retry: state.error.kind === "transient",
			dismissible: false,
		});
	}
	return notices;
});

// §14 F2 - ColumnsButton owns useStorage('jarvis-cols-'+storageKey) and pushes
// the hidden-key list up; ListPage filters the visible columns from it.
const hiddenKeys = ref([]);
// Numeric widths compile to bare `Nfr` grid tracks whose implicit minimum is
// min-content, so one sentence-length cell stretches the whole list into
// horizontal scroll and `truncate` never bites. minmax(0, Nfr) restores real
// flexible tracks (and therefore working truncation) for every list.
const visibleColumns = computed(() =>
	(props.columns || [])
		.filter((c) => !hiddenKeys.value.includes(c.key))
		.map((c) => (typeof c.width === "number" ? { ...c, width: `minmax(0, ${c.width}fr)` } : c))
);

// ── quick filters (toolbar-left strip; selects apply immediately, text 500ms) ──
function quickValue(qf) {
	const v = props.filters ? props.filters[qf.key] : undefined;
	return v == null ? "" : v;
}
function applyQuick(qf, value) {
	const next = { ...(props.filters || {}) };
	if (value === "" || value == null) delete next[qf.key];
	else next[qf.key] = value;
	emit("update:filters", next);
}
const textTimers = {};
function onQuickText(qf, value) {
	clearTimeout(textTimers[qf.key]);
	textTimers[qf.key] = setTimeout(() => applyQuick(qf, value), 500);
}
onBeforeUnmount(() => {
	for (const key of Object.keys(textTimers)) clearTimeout(textTimers[key]);
});
</script>
