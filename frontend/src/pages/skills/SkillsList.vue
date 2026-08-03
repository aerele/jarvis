<template>
	<ListPage
		:breadcrumbs="[{ label: 'Skills', route: { name: 'SkillsList' } }]"
		:columns="columns"
		:rows="rows"
		:loading="loading"
		:total="total"
		:has-more="hasMore"
		:quick-filters="quickFilters"
		:filter-state="filterState"
		:filters="filters"
		:sort-options="sortOptions"
		:sort="sort"
		:page-length="pageLength"
		:default-sort="DEFAULT_SORT"
		:selectable="true"
		:get-row-route="getRowRoute"
		storage-key="skills"
		:empty-state="{
			icon: 'zap',
			title: 'No Skills Found',
			description: 'Create a skill to teach Jarvis reusable instructions.',
		}"
		@update:filters="setFilters"
		@update:filter-clauses="setClauses"
		@request-filter-schema="requestSchema"
		@dismiss-filter-notice="dismissFilterNotice"
		@update:sort="(s) => setSort(s.field, s.dir)"
		@update:page-length="(v) => (pageLength = v)"
		@load-more="loadMore"
		@refresh="resetLoad"
	>
		<template #right-header>
			<Button
				variant="solid"
				label="New Skill"
				iconLeft="plus"
				@click="router.push({ name: 'SkillNew' })"
			/>
		</template>

		<!-- apply-pipeline status (renders nothing while idle) -->
		<template #banner>
			<SyncPill ref="syncPill" class="mb-3" />
		</template>

		<template #cell-skill_name="{ row }">
			<div class="flex items-center gap-2 overflow-hidden">
				<div class="truncate text-base font-medium text-ink-gray-9">
					{{ row.skill_name }}
				</div>
				<Tooltip
					v-if="row.scope === 'Personal'"
					text="Personal skill - only you; never pushed to the shared assistant"
				>
					<Badge variant="subtle" theme="green" label="Personal" />
				</Tooltip>
			</div>
		</template>

		<template #cell-owner_display="{ row }">
			<div class="flex items-center gap-2 overflow-hidden">
				<Avatar
					size="sm"
					:label="row.mine ? session.user || 'You' : row.shared_by || '?'"
				/>
				<div class="truncate text-base">
					{{ row.mine ? "You" : row.shared_by || "another user" }}
				</div>
			</div>
		</template>

		<template #cell-shared_count="{ row }">
			<div class="flex w-full items-center justify-center">
				<Badge
					v-if="row.mine && row.shared_count > 0"
					variant="subtle"
					theme="gray"
					:label="`${row.shared_count} user${row.shared_count === 1 ? '' : 's'}`"
				/>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</div>
		</template>

		<template #cell-enabled="{ row }">
			<Badge
				variant="subtle"
				:theme="row.enabled ? 'green' : 'gray'"
				:label="row.enabled ? 'Enabled' : 'Disabled'"
			/>
		</template>

		<template #cell-modified="{ row }">
			<div class="flex w-full items-center justify-end">
				<Tooltip :text="exactDate(row.modified)">
					<div class="truncate text-base">{{ timeAgo(row.modified) }}</div>
				</Tooltip>
			</div>
		</template>

		<template #select-actions="{ selections, unselectAll }">
			<Dropdown
				:options="[
					{ label: 'Delete', onClick: () => bulkDelete(selections, unselectAll) },
				]"
			>
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
		</template>
	</ListPage>
</template>

<script setup>
// Skills list - DESIGN-V3 §5.6: scope/enabled quick filters, sync-pill banner
// (apply pipeline status, 3s poll while pending), bulk delete with skip
// reasons (owner-only rows), rows → /skills/:id, New Skill → /skills/new.
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Button, Badge, Avatar, Tooltip, Dropdown, toast, confirmDialog } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import { useListPage } from "@/composables/useListPage";
import SyncPill from "./SyncPill.vue";
import { session } from "@/data/session";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as api from "@/api";
import * as apiSkills from "@/api/skills";

const router = useRouter();
// /skills is a TABBED shell (Skills | Learning), so the `fv2` query param is
// shared with a sibling tab; the payload carries its view key and a foreign one
// is ignored rather than half-applied (judgment C08-7).
const route = useRoute();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── list config ──────────────────────────────────────────────────────────────
// "Ownership", not "Scope": scope is the Org/Personal skill field (shown as a
// badge on the Name column); this filter is about whose skills you're viewing.
const OWNERSHIP_OPTIONS = [
	{ label: "All skills", value: "" },
	{ label: "Mine", value: "mine" },
	{ label: "Shared with me", value: "shared" },
];
const ENABLED_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Enabled", value: "1" },
	{ label: "Disabled", value: "0" },
];

const columns = [
	{ label: "Name", key: "skill_name", width: 2 },
	{ label: "Description", key: "description", width: 3 },
	{ label: "Owner", key: "owner_display", width: 1 },
	{ label: "Shared", key: "shared_count", width: "6rem", align: "center" },
	{ label: "Status", key: "enabled", width: "7rem" },
	{ label: "Updated", key: "modified", width: "8rem", align: "right" },
];

// search rides the quick-filter strip (ListPage has no separate search box);
// it lives in the filters object so the input stays controlled, and fetchFn
// moves it onto the envelope's `search` param (§11 parity: skills search).
const quickFilters = [
	{ key: "search", label: "Search skills", type: "text" },
	{ key: "scope", label: "Ownership", type: "select", options: OWNERSHIP_OPTIONS },
	{ key: "enabled", label: "Status", type: "select", options: ENABLED_OPTIONS },
];
// plan 08 wave 1: the curated `filterDefs` array is GONE as the filter catalog.
// The Filter panel is now generated from the server schema for view_key
// "skills", so every readable field of Jarvis Custom Skill (plus its standard
// fields) is filterable — `user_invocable`, which used to need this array, is
// simply one of them now.
//
// `enabled` is the one quick control with a 1:1 canonical field behind it, so it
// synchronises both ways with the panel. `scope` stays legacy-only on purpose:
// mine|shared is an OWNERSHIP pseudo-filter over a server-authored share list,
// not the doctype's `scope` field (see list_registry's note on this view).
const QUICK_CLAUSES = { enabled: "enabled" };

const sortOptions = [
	{ label: "Name", value: "skill_name" },
	{ label: "Updated", value: "modified" },
	{ label: "Enabled", value: "enabled" },
];
const DEFAULT_SORT = { field: "skill_name", dir: "asc" };

const {
	rows,
	total,
	hasMore,
	loading,
	filters,
	setFilters,
	sort,
	setSort,
	pageLength,
	resetLoad,
	loadMore,
	filterState,
	setClauses,
	requestSchema,
	dismissFilterNotice,
} = useListPage({
	fetchFn: (p) => {
		// the backend whitelists filter keys and throws on "search" - strip it
		// out of filters and send it as the envelope's search param instead
		const { search: q, ...rest } = p.filters || {};
		return api.listCustomSkillsPage({ ...p, search: q || p.search || "", filters: rest });
	},
	defaultSort: DEFAULT_SORT,
	storageKey: "skills",
	viewKey: "skills",
	quickClauses: QUICK_CLAUSES,
	// The view's identity (it mirrors this view's list_registry entry), NOT a
	// field catalog — that only ever comes from the server. It lets a quick
	// filter become a canonical, shareable clause before the catalog is fetched.
	rootDoctype: "Jarvis Custom Skill",
	route,
	router,
});

function getRowRoute(row) {
	return { name: "SkillDetail", params: { id: row.name } };
}

// ── bulk delete (owner rows only; server skips the rest with reasons) ────────
const syncPill = ref(null);

function bulkDelete(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length) return;
	confirmDialog({
		title: `Delete ${names.length} skill${names.length === 1 ? "" : "s"}?`,
		message:
			"Deletes the selected skills you own and removes them from your assistant. Rows shared with you are skipped.",
		onConfirm: async ({ hideDialog }) => {
			try {
				const res = (await apiSkills.deleteCustomSkillsBulk(names)) || {};
				const skipped = res.skipped || [];
				const deleted = res.deleted != null ? res.deleted : names.length - skipped.length;
				if (skipped.length) {
					const reasons = [...new Set(skipped.map((s) => s.reason || "skipped"))].join(
						", "
					);
					toast.create({
						message: `Deleted ${deleted} (skipped ${skipped.length}: ${reasons})`,
						type: "info",
					});
				} else {
					toast.success(`Deleted ${deleted} skill${deleted === 1 ? "" : "s"}`);
				}
				unselectAll();
				hideDialog();
				resetLoad();
				// the server enqueued one skills-apply at the end (§8.3) - show it
				syncPill.value && syncPill.value.checkNow();
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}
</script>
