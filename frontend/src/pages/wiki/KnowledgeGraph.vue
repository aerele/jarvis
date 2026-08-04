<template>
	<div class="kg flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="breadcrumbs" />
			</template>
			<template #right-header>
				<Button
					icon="refresh-cw"
					:tooltip="'Refresh'"
					:loading="!state.loaded"
					@click="load"
				/>
			</template>
		</LayoutHeader>
		<div class="kg-head">
			<h5>Knowledge Graph</h5>
			<span v-if="state.loaded && pageCount" class="text-muted"
				>{{ pageCount }} pages · {{ linkCount }} links</span
			>
		</div>

		<div v-if="!state.loaded" class="kg-skel"></div>
		<div v-else-if="state.error" class="kg-err">
			{{ state.error }} <button class="kg-btn" @click="load">Retry</button>
		</div>
		<div v-else-if="!rawPageCount" class="kg-empty text-muted">
			<i
				>No wiki pages yet — create some under the Wiki tab, then come back to connect and
				explore them here.</i
			>
		</div>

		<div v-else class="kg-layout min-h-0 flex-1">
			<div class="kg-main">
				<div class="kg-tools">
					<FilterBar
						:mode="state.mode"
						:overlay="state.overlay"
						:shown="pageCount"
						:total="rawPageCount"
						@update:mode="(v) => (state.mode = v)"
						@update:overlay="(v) => (state.overlay = v)"
					/>
					<input
						class="kg-search"
						type="text"
						placeholder="Search nodes…"
						v-model="state.search"
					/>
					<span v-if="state.focus" class="kg-focus">
						<Badge theme="blue" variant="subtle" size="sm" label="Focused" />
						<Button
							variant="ghost"
							size="sm"
							label="Clear"
							@click="state.focus = null"
						/>
					</span>
				</div>
				<ExclusionRules
					class="kg-excl"
					:page-types="pageTypes"
					:excluded="state.excluded"
					@update:excluded="onExclude"
				/>
				<Graph3D
					:data="filtered"
					:metrics="state.analysis.metrics"
					:mode="state.mode"
					:dark="dark"
					@node-click="onNode"
				/>
			</div>
			<div class="kg-side">
				<DetailPanel
					:node="state.selected"
					:metrics="state.analysis.metrics"
					:communities="state.analysis.communities"
					@focus="(id) => (state.focus = id)"
				/>
				<AnalysisTabs
					:analysis="state.analysis"
					:nodes="baseData.nodes"
					:actions="state.actions"
					:history="state.history"
					:can-act="!state.linking"
					@pick="pickId"
					@add-link="onAddLink"
				/>
			</div>
		</div>

		<div v-if="state.toast" class="kg-toast">{{ state.toast }}</div>
	</div>
</template>

<script setup>
// Tenant Knowledge Graph — the productive tool. Fetch caller-scoped graph → apply
// exclusion → worker analysis (structure + TF-IDF similarity) → 3D graph + the
// four analysis tabs. Productive loop: accept a suggested connection → add_wiki_link
// (durable, out-of-body) → refetch → the edge appears.
//
// That loop used to be described here and switched off in the template: the page
// passed :show-actions / :can-act / :show-priority / :show-actions-tab as false,
// three of them overriding a package default of true, so the "+ link" button, the
// Actions tab and the priority strip never rendered and `addWikiLink` had no
// caller anywhere in the SPA. The props are gone and `add-link` is wired.
//
// `canAct` is not a permission claim. add_wiki_link re-checks edit rights on the
// source and read rights on the target server-side, and per-page rights vary by
// scope, so the button is offered to everyone and a denial comes back as a toast
// (same contract the wiki save/archive buttons already follow).
import { reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { Badge, Breadcrumbs, Button } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import {
	Graph3D,
	FilterBar,
	DetailPanel,
	AnalysisTabs,
	ExclusionRules,
	runAnalysis,
	computeActions,
	overlayFilter,
	egoGraph,
	searchGraph,
} from "wiki-graph-core";
import { addWikiLink, getWikiGraph, getWikiGraphHistory } from "@/api/wiki";
import { useJarvisTheme } from "@/theme";

// Same breadcrumb + persistent "Open ERPNext Desk" top bar the other Skills
// tabs get; LayoutHeader supplies the ⧉ button, so the graph tab no longer
// needs its own inline one.
const breadcrumbs = [
	{ label: "Skills", route: { name: "SkillsList" } },
	{ label: "Knowledge Graph" },
];

const PAGE_TYPES = [
	"Customer",
	"Supplier",
	"Item",
	"Process",
	"Doctype",
	"Exception",
	"Integration",
	"People",
	"Org",
];

const state = reactive({
	data: { nodes: [], edges: [] },
	analysis: { metrics: {}, lists: {}, communities: {} },
	// Full empty shape, not {}: ActionsTab reads actions.stale.length directly
	// (no per-key guard), and it is reachable now that the Actions tab renders.
	actions: { stale: [], orphans: [], busFactor: [], duplicates: [], suggest: [] },
	history: [],
	loaded: false,
	error: null,
	selected: null,
	mode: "kind",
	overlay: "knowledge",
	search: "",
	focus: null,
	toast: "",
	// One curation in flight at a time: the "+ link" buttons are plain package
	// buttons with no disabled state, so this is what stops a double-click from
	// firing two writes against a row add_wiki_link locks anyway.
	linking: false,
	excluded: readExcluded(),
});
// Reactive theme so the 3D graph re-colours on a live light/dark toggle. Reading
// data-theme once at setup left the canvas stuck on the theme that was active
// when the page mounted.
const { effectiveDark: dark } = useJarvisTheme();

function readExcluded() {
	try {
		return JSON.parse(localStorage.getItem("wg-excl") || "[]");
	} catch (_) {
		return [];
	}
}
function onExclude(list) {
	state.excluded = list;
	try {
		localStorage.setItem("wg-excl", JSON.stringify(list));
	} catch (_) {}
}

function applyExclusion(data, excluded) {
	if (!excluded || !excluded.length) return data;
	const ex = new Set(excluded);
	const nodes = (data.nodes || []).filter((n) => n.kind !== "page" || !ex.has(n.page_type));
	const ids = new Set(nodes.map((n) => n.id));
	const edges = (data.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target));
	return { nodes, edges, gaps: data.gaps, co_read: data.co_read };
}

const baseData = computed(() => applyExclusion(state.data, state.excluded));
const rawPageCount = computed(
	() => (state.data.nodes || []).filter((n) => n.kind === "page").length
);
const pageCount = computed(() => baseData.value.nodes.filter((n) => n.kind === "page").length);
const linkCount = computed(() => baseData.value.edges.filter((e) => e.kind === "links-to").length);
const pageTypes = computed(() => {
	const present = new Set(
		(state.data.nodes || []).filter((n) => n.kind === "page").map((n) => n.page_type)
	);
	return PAGE_TYPES.filter((t) => present.has(t));
});
const filtered = computed(() => {
	const g = overlayFilter(baseData.value, state.overlay);
	if (state.focus) return egoGraph(g, state.focus, 2);
	if (state.search) return searchGraph(g, state.search, 1);
	return g;
});

let _seq = 0;
async function recompute() {
	const token = ++_seq;
	const data = baseData.value;
	const analysis = await runAnalysis(data);
	if (token !== _seq) return; // a newer recompute superseded us
	state.analysis = analysis;
	state.actions = computeActions(data, analysis);
}

// Refetch + re-analyse in place. Kept separate from `load` so a curation can
// refresh the graph WITHOUT dropping the page back to its skeleton: the point of
// the loop is watching the new edge appear, not watching the view blank out.
async function refetchGraph() {
	const d = await getWikiGraph();
	state.data = d || { nodes: [], edges: [] };
	await recompute();
}

async function load() {
	state.loaded = false;
	state.error = null;
	try {
		// Measured evolution history (best-effort; empty until the daily job runs
		// or the doctype is migrated in — the Evolution tab reconstructs meanwhile).
		getWikiGraphHistory()
			.then((h) => {
				state.history = Array.isArray(h) ? h : [];
			})
			.catch(() => {});
		await refetchGraph();
	} catch (e) {
		state.error = (e && e.message) || String(e);
	} finally {
		state.loaded = true;
	}
}

let _toastTimer = null;
function toast(message) {
	state.toast = message;
	clearTimeout(_toastTimer);
	_toastTimer = setTimeout(() => (state.toast = ""), 4000);
}

// Node ids are "page:<slug>"; the node itself carries the real slug, so prefer it
// and fall back to stripping the prefix.
function slugOf(id) {
	const node = (state.data.nodes || []).find((n) => n.id === id);
	return (node && node.slug) || String(id || "").replace(/^page:/, "");
}

// The productive loop: a suggested pair from SuggestPanel/ActionsTab -> the
// curated link -> a refetch that shows the edge. add_wiki_link is idempotent and
// row-locked, so a repeat is a no-op rather than a duplicate.
async function onAddLink(suggestion) {
	if (state.linking || !suggestion) return;
	const from = slugOf(suggestion.a);
	const to = slugOf(suggestion.b);
	if (!from || !to || from === to) return;

	state.linking = true;
	try {
		await addWikiLink(from, to);
	} catch (e) {
		toast((e && e.message) || "Could not add that link.");
		return;
	} finally {
		state.linking = false;
	}

	// Past here the link IS stored, so a failing refetch only leaves the view
	// stale. Reporting that as "could not add that link" would be a lie the user
	// acts on; the header Refresh button recovers the view.
	toast(`Linked ${suggestion.aLabel || from} to ${suggestion.bLabel || to}`);
	try {
		await refetchGraph();
	} catch (_) {}
}

function onNode(node) {
	state.selected = node;
}
function pickId(id) {
	const n = (state.data.nodes || []).find((x) => x.id === id);
	if (n) state.selected = n;
}
watch(() => state.excluded, recompute);
onMounted(load);
onBeforeUnmount(() => clearTimeout(_toastTimer));
</script>

<style scoped>
/* The wiki-graph-core child components (FilterBar, ExclusionRules, DetailPanel,
   AnalysisTabs + its panels) style themselves via legacy var names that were
   never defined anywhere, so they always fell back to hardcoded LIGHT literals
   and ignored dark mode. They were built to inherit these from a host: define
   them here on the page root, mapped to the frappe-ui semantic tokens that
   auto-flip under [data-theme="dark"]. Custom properties inherit across Vue
   scoped-style boundaries, so every descendant picks these up.
   --bg-blue is intentionally left to its own #4c9aff fallback: it is used as a
   solid fill with white text AND as accent text, and reads correctly on both
   themes — remapping it to a --surface-blue-* token would wash out one theme. */
.kg {
	padding: 4px 2px;
	--card-bg: var(--surface-white);
	--border-color: var(--outline-gray-2);
	--control-bg: var(--surface-gray-2);
	--text-muted: var(--ink-gray-5);
	--text-color: var(--ink-gray-9);
}
.kg-head {
	display: flex;
	align-items: baseline;
	gap: 10px;
	margin-bottom: 6px;
}
.kg-layout {
	display: grid;
	grid-template-columns: minmax(0, 1fr) 340px;
	gap: 16px;
}
/* min-width:0 lets the graph column shrink to its track — without it the
   3d-force-graph canvas (an explicit-pixel-width element) forces the column
   wider than the viewport and shoves the side panel off-screen. */
.kg-main {
	min-width: 0;
}
.kg-side {
	display: flex;
	flex-direction: column;
	gap: 14px;
	overflow-y: auto;
}
.kg-tools {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 12px;
	margin-bottom: 6px;
}
.kg-excl {
	margin-bottom: 8px;
}
/* Themed via the frappe-ui semantic tokens (this page renders under the
   data-theme app shell, not a jv-root). The old var(--border-color / --bg-blue /
   --card-bg / --control-bg) names were never defined, so every rule fell back to
   its hardcoded light value and the page ignored dark mode. */
.kg-search {
	font-size: 12px;
	padding: 4px 10px;
	border: 1px solid var(--outline-gray-2);
	border-radius: 6px;
	min-width: 180px;
	background: var(--surface-white);
	color: var(--ink-gray-9);
}
/* Was a hand-rolled pill: `background: var(--surface-blue-2); color: #fff`. But
   --surface-blue-2 is the PALE badge tint (#E6F4FF light), meant to pair with
   dark ink — white on it is 1.12:1, i.e. invisible, in the default theme. Rather
   than hand-tune another bespoke colour pair, this now uses the house components:
   a frappe-ui Badge (the §3.6 badge recipe, consistent with every other badge in
   the app) plus a real ghost Button for the action — which also makes "clear"
   keyboard-operable and focus-ringed, unlike the <a href="#"> it replaced. */
.kg-focus {
	display: inline-flex;
	align-items: center;
	gap: 4px;
}
.kg-err {
	color: var(--ink-red-4);
	padding: 16px 0;
}
.kg-empty {
	padding: 24px 4px;
	font-size: 13px;
}
.kg-btn {
	font-size: 12px;
	padding: 3px 10px;
	border: 1px solid var(--outline-gray-2);
	border-radius: 6px;
	background: var(--surface-white);
	color: var(--ink-gray-9);
	cursor: pointer;
	margin-left: 8px;
}
.kg-skel {
	height: 60vh;
	border-radius: 8px;
	background: var(--surface-gray-2);
	animation: kg-pulse 1.4s ease-in-out infinite;
}
@keyframes kg-pulse {
	0%,
	100% {
		opacity: 0.5;
	}
	50% {
		opacity: 0.9;
	}
}
/* inverted bubble: surface-gray-7 is dark in light mode / light in dark mode, so
   the toast stays legible against either background */
.kg-toast {
	position: fixed;
	bottom: 20px;
	left: 50%;
	transform: translateX(-50%);
	background: var(--surface-gray-7);
	color: var(--surface-white);
	padding: 8px 16px;
	border-radius: 8px;
	font-size: 13px;
	z-index: 50;
}
/* Only stack (panel below the graph) on genuinely narrow screens. The old
   1100px breakpoint hid the analysis panel off-screen on ordinary
   retina-scaled laptop widths — it read as "the panel is missing". */
@media (max-width: 760px) {
	.kg-layout {
		grid-template-columns: 1fr;
	}
}
</style>
