<template>
	<SettingsPane
		title="Agent audit"
		description="Everything the assistant changed across your team — the durable record."
	>
		<!-- D2 bypass + retention disclosure, bundled into one honest line. -->
		<p class="text-p-sm text-ink-gray-5">
			Audit view — lists changes the assistant made across your team, including records you
			may not have permission to open. Retained 90 days.
		</p>

		<!-- Summary strip: cheap headline aggregates for the last 7 days. -->
		<div
			v-if="summary"
			class="mt-3 flex flex-wrap items-baseline gap-x-1.5 gap-y-1 text-p-sm text-ink-gray-6"
		>
			<span class="font-medium text-ink-gray-8">This week:</span>
			<span
				><span class="font-semibold text-ink-gray-8">{{ summary.writes }}</span>
				{{ plural(summary.writes, "write") }}</span
			>
			<span aria-hidden="true">·</span>
			<span
				><span class="font-semibold text-ink-gray-8">{{ summary.failed }}</span>
				failed</span
			>
			<span aria-hidden="true">·</span>
			<span
				>across <span class="font-semibold text-ink-gray-8">{{ summary.actors }}</span>
				{{ plural(summary.actors, "person", "people") }}</span
			>
		</div>

		<!-- Filters -->
		<div class="mt-4 flex flex-wrap items-end gap-3">
			<FormControl
				type="text"
				label="Person"
				placeholder="Filter by user…"
				class="w-56"
				:modelValue="actor"
				@update:modelValue="onActorInput"
			/>
			<FormControl
				type="select"
				label="Outcome"
				class="w-40"
				:options="OUTCOME_OPTIONS"
				:modelValue="outcome"
				@update:modelValue="onOutcomeChange"
			/>
		</div>

		<!-- Load failure keeps its own inline recovery (SettingsPane's error slot
		     is a String prop for an action, not a load state). -->
		<div v-if="loadError" class="flex flex-col items-center gap-3 py-12 text-center">
			<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">Could not load the audit trail.</span>
			<Button
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="loading"
				@click="reload"
			/>
		</div>

		<p v-else-if="loading && !rows.length" class="mt-6 text-p-base text-ink-gray-6">
			Loading…
		</p>

		<div v-else-if="!rows.length" class="flex flex-col items-center gap-2 py-12 text-center">
			<FeatherIcon name="shield" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">No agent activity yet.</span>
		</div>

		<template v-else>
			<div
				class="mt-4 grid items-center gap-3.5 pb-2 text-xs font-medium text-ink-gray-5"
				:style="gridCols"
			>
				<div>When</div>
				<div>Person</div>
				<div>Action</div>
				<div>Record</div>
				<div>Outcome</div>
			</div>

			<div
				v-for="r in rows"
				:key="r.name"
				class="grid items-center gap-3.5 border-t py-3"
				:style="gridCols"
			>
				<div class="text-xs text-ink-gray-5" :title="r.at">
					{{ r.at ? timeAgo(r.at) : "-" }}
				</div>

				<div class="min-w-0">
					<div class="truncate text-sm text-ink-gray-8" :title="r.actor">
						{{ r.actor_name || r.actor }}
					</div>
				</div>

				<div class="min-w-0 truncate text-sm text-ink-gray-7" :title="toolLabel(r.tool)">
					{{ toolLabel(r.tool)
					}}<span v-if="r.bulk_count > 1" class="text-ink-gray-5">
						×{{ r.bulk_count }}</span
					>
				</div>

				<div class="min-w-0 truncate text-sm">
					<a
						v-if="r.ref_doctype && r.ref_name"
						:href="recordUrl(r)"
						target="_blank"
						rel="noopener"
						class="text-ink-gray-8 underline decoration-ink-gray-3 underline-offset-2 hover:decoration-ink-gray-6"
						:title="`Open ${r.ref_doctype} ${r.ref_name} in Desk (may require permission)`"
					>
						{{ r.ref_name }}
					</a>
					<span v-else-if="r.ref_doctype" class="text-ink-gray-6" :title="r.ref_doctype">
						{{ r.ref_doctype
						}}<span v-if="r.bulk_count > 1"> ({{ r.bulk_count }})</span>
					</span>
					<span v-else class="text-ink-gray-4">—</span>
				</div>

				<div>
					<Badge :theme="outcomeTheme(r.outcome)" :label="outcomeLabel(r.outcome)" />
				</div>
			</div>

			<div v-if="hasMore" class="flex justify-center pt-4">
				<Button variant="subtle" label="Load more" :loading="loading" @click="loadMore" />
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
// Manager-facing agent-write audit feed (act-now #3). Gated at the SettingsDialog
// level by window.is_jarvis_admin; the server re-checks require_jarvis_admin() on
// every call, so the client gate only hides the nav item. Rows are metadata only
// — there is no conversation content to render.
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, FeatherIcon, FormControl } from "frappe-ui";
import { timeAgo } from "@/utils/datetime";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import * as api from "@/api";

const PAGE_LENGTH = 50;
const OUTCOME_OPTIONS = [
	{ label: "All outcomes", value: "" },
	{ label: "Applied", value: "applied" },
	{ label: "Failed", value: "failed" },
	{ label: "Discarded", value: "discarded" },
];

const rows = ref([]);
const summary = ref(null);
const loading = ref(false);
const loadError = ref(false);
const hasMore = ref(false);
const start = ref(0);
const actor = ref("");
const outcome = ref("");

const gridCols = { gridTemplateColumns: "0.9fr 1.3fr 1.3fr 1.6fr 0.9fr" };

function plural(n, one, many) {
	return Number(n) === 1 ? one : many || one + "s";
}

// snake_case tool name → "Sentence case" for the manager (no brittle label map;
// the raw tool name is a fine fallback for a tool this simple rule doesn't cover).
function toolLabel(tool) {
	const t = String(tool || "")
		.replace(/_/g, " ")
		.trim();
	return t ? t.charAt(0).toUpperCase() + t.slice(1) : "-";
}

const OUTCOME_THEME = { applied: "green", failed: "red", discarded: "gray" };
function outcomeTheme(o) {
	return OUTCOME_THEME[o] || "gray";
}
function outcomeLabel(o) {
	return o ? o.charAt(0).toUpperCase() + o.slice(1) : "-";
}

// Desk route: /app/<doctype-slug>/<name>. Slug mirrors frappe.router.slug
// (lowercase, spaces → hyphens); the name is URL-encoded (record ids can carry
// spaces and slashes).
function recordUrl(r) {
	const slug = String(r.ref_doctype || "")
		.toLowerCase()
		.replace(/ /g, "-");
	return `/app/${slug}/${encodeURIComponent(r.ref_name)}`;
}

async function fetchPage({ reset }) {
	loading.value = true;
	if (reset) loadError.value = false;
	try {
		const res = await api.adminListAgentWrites({
			actor: actor.value || undefined,
			outcome: outcome.value || undefined,
			start: reset ? 0 : start.value,
			page_length: PAGE_LENGTH,
		});
		if (!res || res.ok === false) {
			if (reset) loadError.value = true;
			return;
		}
		const data = res.data || {};
		const page = data.rows || [];
		if (reset) {
			rows.value = page;
			start.value = page.length;
		} else {
			rows.value = rows.value.concat(page);
			start.value += page.length;
		}
		hasMore.value = !!data.has_more;
	} catch (e) {
		if (reset) loadError.value = true;
	} finally {
		loading.value = false;
	}
}

function reload() {
	fetchPage({ reset: true });
}
function loadMore() {
	fetchPage({ reset: false });
}

async function loadSummary() {
	try {
		const res = await api.adminAgentWriteSummary();
		summary.value = res && res.ok !== false ? res.data : null;
	} catch (e) {
		summary.value = null; // the strip is a nicety; never block the table on it
	}
}

// Debounce the free-text person filter so typing doesn't fire a request per
// keystroke (the outcome select refetches immediately — a discrete choice).
let actorTimer = null;
function onActorInput(v) {
	actor.value = v;
	if (actorTimer) clearTimeout(actorTimer);
	actorTimer = setTimeout(() => fetchPage({ reset: true }), 300);
}
function onOutcomeChange(v) {
	outcome.value = v;
	fetchPage({ reset: true });
}

onBeforeUnmount(() => {
	if (actorTimer) clearTimeout(actorTimer);
});

onMounted(() => {
	reload();
	loadSummary();
});
</script>
