<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: 'Agents', route: { name: 'AgentsList' } }]" />
			</template>
			<template #right-header>
				<!-- Reviewer/SM apply pipeline: prominent while the catalog is dirty,
				     SyncPill-style pending/failed states while an apply runs.
				     Gated on the skill-reviewer capability (what apply_agents needs),
				     NOT the SM-only cross-owner admin overview. -->
				<div v-if="canApply" class="flex items-center gap-2">
					<!-- duration lives IN the badge text - tooltips are invisible to
					     keyboard/SR users -->
					<Badge v-if="sync.pending" theme="orange" variant="subtle" size="lg">
						<template #prefix>
							<!-- currentColor so the spinner takes the orange badge tone,
							     not the brand accent (JvSpinner's default). -->
							<JvSpinner color="currentColor" />
						</template>
						Applying agents - ~30s, one restart
					</Badge>
					<template v-else>
						<Badge
							v-if="syncFailed"
							theme="red"
							variant="subtle"
							label="Apply failed"
						/>
						<Badge
							v-else-if="sync.dirty"
							theme="orange"
							variant="subtle"
							label="Changes pending"
						/>
						<Button
							:variant="sync.dirty || syncFailed ? 'solid' : 'subtle'"
							:label="syncFailed ? 'Retry apply' : 'Apply catalog changes'"
							iconLeft="upload-cloud"
							:loading="applying"
							@click="applyCatalog"
						/>
					</template>
				</div>
			</template>
		</LayoutHeader>

		<!-- 4 hash-synced tabs (no hash = Featured; #available/#installed/#activity) -->
		<TabBar class="shrink-0" :tabs="TABS" :model-value="tab" @update:model-value="setTab" />

		<!-- persistent apply-failure banner: the reason must survive the toast and
		     be reachable without hovering a badge (keyboard/SR access). x-circle, not
		     Banner's fixed error icon: matches AgentActivityTab's own run_failed
		     convention, which Banner has no prop to select. -->
		<div
			v-if="canApply && syncFailed && !sync.pending"
			class="mx-5 mt-3 flex shrink-0 items-start gap-2 rounded-lg border border-outline-red-1 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
		>
			<FeatherIcon name="x-circle" class="size-4 shrink-0" />
			<span>{{ syncFailureMessage }}</span>
		</div>

		<!-- Activity: self-contained feed (own search + pagination) -->
		<AgentActivityTab v-if="tab === 'activity'" class="min-h-0 flex-1" />

		<!-- Featured / Available / Installed: search · category · sort → card grid -->
		<template v-else>
			<div class="flex flex-wrap items-center gap-2 px-5 pt-4">
				<FormControl
					type="text"
					class="w-60"
					placeholder="Search agents"
					:modelValue="search"
					@update:modelValue="(v) => (search = v)"
				>
					<template #prefix>
						<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
					</template>
				</FormControl>
				<div class="flex-1" />
				<FormControl
					type="select"
					class="w-52"
					:options="categoryOptions"
					:modelValue="category"
					@update:modelValue="(v) => (category = v)"
				/>
				<FormControl
					type="select"
					class="w-44"
					:options="SORT_OPTIONS"
					:modelValue="sortChoice"
					@update:modelValue="(v) => (sortChoice = v)"
				/>
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto px-5 pb-8 pt-4">
				<div
					v-if="(loading || probingCatalog) && !rows.length"
					class="py-10 text-center text-sm text-ink-gray-5"
				>
					Loading the catalog…
				</div>
				<div
					v-else-if="error && !rows.length"
					class="py-10 text-center text-sm text-ink-red-4"
				>
					{{ error }}
				</div>
				<div
					v-else-if="!rows.length"
					class="flex flex-col items-center gap-1 py-16 text-center"
				>
					<FeatherIcon name="cpu" class="size-7.5 text-ink-gray-5" />
					<span class="mt-2 text-lg font-medium text-ink-gray-8">{{
						emptyState.title
					}}</span>
					<span class="text-p-base text-ink-gray-6">{{ emptyState.description }}</span>
					<Button
						v-if="emptyState.cta"
						class="mt-3"
						label="Browse available agents"
						@click="setTab('available')"
					/>
				</div>

				<div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
					<div
						v-for="a in rows"
						:key="a.agent_slug"
						role="button"
						tabindex="0"
						class="flex cursor-pointer flex-col rounded-lg border bg-surface-white p-5 transition hover:bg-surface-gray-1 hover:shadow-sm"
						@click="openAgent(a)"
						@keydown.enter.prevent="openAgent(a)"
						@keydown.space.prevent="openAgent(a)"
					>
						<div class="flex items-start gap-3">
							<!-- letter-avatar logo (listing has no image field) -->
							<div
								class="grid h-11 w-11 shrink-0 place-items-center rounded-lg border bg-surface-gray-2 text-base font-semibold text-ink-gray-6"
							>
								{{ logoText(a) }}
							</div>
							<div class="min-w-0 flex-1">
								<div class="flex items-center gap-2">
									<span class="truncate text-base font-semibold text-ink-gray-9">
										{{ a.title }}
									</span>
									<Badge
										v-if="a.status === 'Coming Soon'"
										class="shrink-0"
										variant="subtle"
										theme="blue"
										label="Coming Soon"
									/>
									<Badge
										v-else-if="a.status === 'Deprecated'"
										class="shrink-0"
										variant="subtle"
										theme="red"
										label="Deprecated"
									/>
								</div>
								<div class="truncate text-sm text-ink-gray-5">
									by {{ a.publisher || "Unknown"
									}}<template v-if="a.version"> · v{{ a.version }}</template>
								</div>
							</div>
						</div>

						<p class="mt-3 line-clamp-2 min-h-10 text-base leading-5 text-ink-gray-6">
							{{ a.description }}
						</p>

						<div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
							<Badge
								variant="outline"
								theme="gray"
								:label="categoryTitle(a.category)"
							/>
							<span class="flex items-center gap-1 text-ink-gray-5">
								<FeatherIcon name="download" class="size-3.5" />
								{{ installsLabel(a.install_count) }}
							</span>
							<span
								v-if="a.installed"
								class="flex items-center gap-1 text-ink-green-3"
							>
								<FeatherIcon name="check" class="size-3.5" />
								Installed
							</span>
							<Badge
								v-if="a.update_available"
								variant="subtle"
								theme="orange"
								label="Update available"
							/>
						</div>
					</div>
				</div>
			</div>

			<ListFooter
				v-if="rows.length"
				class="shrink-0 border-t px-5 py-2"
				:modelValue="pageLength"
				:options="{ rowCount: rows.length, totalCount: total }"
				@update:modelValue="(v) => (pageLength = v)"
				@loadMore="loadMore"
			/>
		</template>

		<!-- leave-guard: SM with unapplied catalog changes (dirty) -->
		<Dialog
			v-model="leaveDialog.show"
			:options="{ title: 'Unapplied catalog changes' }"
			@close="resolveLeave(false)"
		>
			<template #body-content>
				<p class="text-p-base text-ink-gray-6">
					You have unapplied catalog changes. Apply them now, or leave anyway? Your
					changes stay saved either way - they only reach the assistant after an Apply.
				</p>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						label="Apply & leave"
						:loading="applying"
						@click="applyAndLeave"
					/>
					<Button label="Leave anyway" @click="resolveLeave(true)" />
					<Button variant="ghost" label="Stay" @click="resolveLeave(false)" />
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
// Agents marketplace listing - /agents (DESIGN-V3 §15.3, marketplace revision).
// Four hash-synced tabs (no hash = Featured; #available/#installed/#activity):
//   Featured / Available / Installed - SERVER-paginated card grids via
//     agents_api.list_agents_page (tab semantics live server-side; envelope
//     {rows,total,has_more,...}) with a Category select + a single sort choice
//     (Most installed / Recently updated / Name). No ratings - deliberately
//     deferred.
//   Activity - the owner's lifecycle feed (AgentActivityTab, self-contained).
// Reviewer/SM header action: prominent "Apply catalog changes" driven by
// get_agents_sync_status().dirty (install/uninstall/enable since the last
// successful Apply), with SyncPill-style pending/failed polling, plus a
// route-leave + beforeunload guard while dirty. Gated on the skill-reviewer
// capability (get_agents_caps().review) since apply_agents needs the reviewer
// set, not System Manager.
import { reactive, ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Badge,
	Breadcrumbs,
	Button,
	Dialog,
	FeatherIcon,
	FormControl,
	ListFooter,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import TabBar from "@/components/list/TabBar.vue";
import AgentActivityTab from "./AgentActivityTab.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { useListPage } from "@/composables/useListPage";
import * as api from "@/api";
import * as agentsApi from "@/api/agents";
import { humaniseSyncStatus } from "@/lib/syncStatus";
import { agentsEmptyState, shouldProbeWholeCatalog } from "@/lib/agentsEmptyState";
import { errHtml } from "@/lib/errors";
import { categoryTitle } from "@/lib/agentCategory";

const route = useRoute();
const router = useRouter();

// ── hash-synced tabs (AgentDetail pattern; no hash = Featured) ───────────────
const TABS = [
	{ label: "Featured", value: "featured" },
	{ label: "Available", value: "available" },
	{ label: "Installed", value: "installed" },
	{ label: "Activity", value: "activity" },
];
const AGENT_TABS = ["featured", "available", "installed"];

const tab = ref("featured");
function applyHash() {
	const h = (route.hash || "").replace(/^#/, "");
	tab.value = TABS.some((t) => t.value === h) ? h : "featured";
}
function setTab(v) {
	if (tab.value === v) return;
	tab.value = v;
	router.push({ hash: v === "featured" ? "" : "#" + v });
}
applyHash();
// back/forward restores the tab
watch(
	() => route.hash,
	() => {
		if (route.name === "AgentsList") applyHash();
	}
);

// ── catalog list: useListPage adapter over list_agents_page ─────────────────
// The adapter closes over tab/category/sortChoice and maps useListPage's
// {search, start, page_length} call onto the marketplace-shaped endpoint;
// its filters/sort_field channel is unused (one category select + one sort
// choice instead of the generic Filter/Sort kit).
const category = ref(""); // "" = all categories
const sortChoice = ref("installs"); // installs | updated | name
const SORT_OPTIONS = [
	{ label: "Most installed", value: "installs" },
	{ label: "Recently updated", value: "updated" },
	{ label: "Name (A-Z)", value: "name" },
];

const { rows, total, loading, error, search, pageLength, resetLoad, loadMore } = useListPage({
	fetchFn: (p) => {
		// Activity owns its own list - never hit the catalog endpoint for it
		// (covers the mount-time fetch when deep-linked to #activity)
		if (!AGENT_TABS.includes(tab.value)) return { rows: [], total: 0, has_more: false };
		return agentsApi.listAgentsPage({
			tab: tab.value,
			category: category.value,
			sort: sortChoice.value,
			search: p.search,
			start: p.start,
			page_length: p.page_length,
		});
	},
	storageKey: "agents",
});

// tab/category/sort changes refetch page 1 (search/page-length are handled
// inside useListPage); switching TO activity is a no-op for the grid
watch([tab, category, sortChoice], ([t]) => {
	if (t === "activity") return;
	resetLoad();
});

// ── category select options (distinct catalog categories) ────────────────────
// One cheap list_agents() call feeds the distinct set - server pages can't (a
// page only sees its slice). sync_agent_listings (agent_catalog.py) already
// sends a real label ("Accounts Payable", not "ap") as `category`, so this
// list IS the display label - no separate frontend title map to keep in sync.
const catalogCategories = ref(null);
async function loadCategories() {
	try {
		const all = (await api.listAgents()) || [];
		const seen = new Set();
		const out = [];
		for (const a of all) {
			const label = a.category || "Other";
			if (seen.has(label)) continue;
			seen.add(label);
			out.push(label);
		}
		if (out.length) catalogCategories.value = out;
	} catch (e) {
		// no fallback list - "All categories" alone still renders a usable select
	}
}
const categoryOptions = computed(() => {
	const labels = catalogCategories.value || [];
	return [
		{ label: "All categories", value: "" },
		...labels.map((l) => ({ label: l, value: l })),
	];
});

// ── display helpers ──────────────────────────────────────────────────────────
function openAgent(a) {
	router.push("/agents/" + a.agent_slug);
}
function logoText(a) {
	return String(a.title || a.agent_slug || "?")
		.slice(0, 2)
		.toUpperCase();
}
function installsLabel(n) {
	const c = n || 0;
	return `${c} install${c === 1 ? "" : "s"}`;
}

// ── Reviewer capability probe (PART 3 remediation): apply_agents needs the
// skill-reviewer set (Jarvis Skill Reviewer | Jarvis Admin | System Manager),
// NOT System Manager. get_agents_caps().review drives the Apply button so a
// reviewer-only System User (no SM) can see and use it; a plain Jarvis User
// gets review=false and no button. Decoupled from the SM-only cross-owner
// getAgentAdminOverview data. ─────────────────────────────────────────────────
const canApply = ref(false);
// The tenant-admin half of the same probe: it decides only which EMPTY-STATE copy
// is truthful (see emptyState), never what the list contains - that filtering is
// server-side in _enriched_catalog.
const canAdminister = ref(false);
async function probeCaps() {
	try {
		const caps = (await agentsApi.getAgentsCaps()) || {};
		canApply.value = !!caps.review;
		canAdminister.value = !!caps.admin;
		if (canApply.value) {
			await loadSyncStatus();
			if (sync.pending) startSyncPoll();
		}
	} catch (e) {
		canApply.value = false;
		canAdminister.value = false;
	}
}

// ── empty states ─────────────────────────────────────────────────────────────
// The decision itself is a pure function in @/lib/agentsEmptyState (tested
// there). Here we only supply what it needs, including the lazily-probed
// "does this caller have ANY agents at all" signal below.
const filtersActive = computed(() => !!(search.value || category.value));
const emptyState = computed(() =>
	agentsEmptyState({
		tab: tab.value,
		filtersActive: filtersActive.value,
		canAdminister: canAdminister.value,
		wholeCatalogEmpty: wholeCatalogEmpty.value,
	})
);

// ── "is this caller's whole catalog empty?" (jarvis#1062) ────────────────────
// null until probed. ONE call answers it for every tab: server-side the
// available tab is `status != Deprecated or installed`, so it is a superset of
// both featured (Published only) and installed - a zero total there means the
// caller can see no agent anywhere, which is the only thing we need to know.
const wholeCatalogEmpty = ref(null);
const probingCatalog = ref(false);

async function probeWholeCatalog() {
	probingCatalog.value = true;
	try {
		const res = (await agentsApi.listAgentsPage({ tab: "available", page_length: 1 })) || {};
		wholeCatalogEmpty.value = (res.total || 0) === 0;
	} catch {
		// Fail to the per-tab copy rather than asserting an access problem we
		// could not confirm.
		wholeCatalogEmpty.value = false;
	} finally {
		probingCatalog.value = false;
	}
}

watch(
	[tab, rows, loading, filtersActive, canAdminister],
	() => {
		if (
			shouldProbeWholeCatalog({
				tab: tab.value,
				loading: loading.value,
				rowCount: rows.value.length,
				filtersActive: filtersActive.value,
				canAdminister: canAdminister.value,
				wholeCatalogEmpty: wholeCatalogEmpty.value,
				probing: probingCatalog.value,
			})
		) {
			probeWholeCatalog();
		}
	},
	{ immediate: true, deep: false }
);

// ── apply pipeline status (SyncPill pattern: 3s poll while pending) ──────────
// dirty = the enabled set changed since the last successful Apply - drives the
// prominent solid button, the "Changes pending" badge and the leave-guard.
const sync = reactive({ pending: false, dirty: false, status: "" });
// Prefix parsing lives in @/lib/syncStatus, not here: this file, SyncPill.vue and
// ReviewTab.vue each carried their own copy of the same regex over the same raw
// last_sync_status strings, which is how the wording drifted apart between them.
const syncHuman = computed(() => humaniseSyncStatus(sync.status));
const syncFailed = computed(() => syncHuman.value.kind === "failed");
const syncFailureReason = computed(() => syncHuman.value.detail);
const syncFailureMessage = computed(
	() =>
		`Applying agents to your assistant failed${
			syncFailureReason.value ? ` - ${syncFailureReason.value}` : ""
		}. Use Retry apply to try again.`
);

let syncTimer = null;
async function loadSyncStatus() {
	try {
		const s = (await api.getAgentsSyncStatus()) || {};
		sync.pending = !!s.pending;
		sync.dirty = !!s.dirty;
		sync.status = s.last_sync_status || "";
	} catch (e) {
		// best-effort: a transient status failure must not break the page
	}
}
function startSyncPoll() {
	if (syncTimer) return;
	syncTimer = setInterval(async () => {
		await loadSyncStatus(); // a successful apply clears dirty server-side
		if (!sync.pending) stopSyncPoll();
	}, 3000);
}
function stopSyncPoll() {
	if (syncTimer) {
		clearInterval(syncTimer);
		syncTimer = null;
	}
}

const applying = ref(false);
async function applyCatalog() {
	if (applying.value) return false;
	applying.value = true;
	try {
		await api.applyAgents();
		toast.success("Applying catalog changes - ~30s, one restart");
		sync.pending = true;
		startSyncPoll();
		return true;
	} catch (e) {
		toast.error(errHtml(e));
		return false;
	} finally {
		applying.value = false;
	}
}

// ── leave-guard: can-apply + dirty (unapplied catalog changes) ───────────────
const leaveDialog = reactive({ show: false, next: null });

onBeforeRouteLeave((to, from, next) => {
	// drilling into /agents/:slug stays inside the agents area - don't nag
	if (!canApply.value || !sync.dirty || String(to.path || "").startsWith("/agents"))
		return next();
	leaveDialog.next = next;
	leaveDialog.show = true;
});

function resolveLeave(go) {
	const n = leaveDialog.next;
	leaveDialog.next = null; // idempotent: Dialog @close re-fires after buttons
	leaveDialog.show = false;
	if (n) n(go);
}

async function applyAndLeave() {
	const ok = await applyCatalog();
	// apply request failed → stay so the SM can retry (error already toasted)
	resolveLeave(ok);
}

// hard reloads / tab close while dirty → native browser prompt
function onBeforeUnload(e) {
	if (canApply.value && sync.dirty) {
		e.preventDefault();
		e.returnValue = ""; // Chrome requires returnValue to show the prompt
	}
}

onMounted(() => {
	probeCaps();
	loadCategories();
	window.addEventListener("beforeunload", onBeforeUnload);
});
onBeforeUnmount(() => {
	window.removeEventListener("beforeunload", onBeforeUnload);
	stopSyncPoll();
});
</script>
