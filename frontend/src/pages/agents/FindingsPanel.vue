<template>
	<div class="mx-auto w-full max-w-3xl px-6 py-6">
		<!-- run header -->
		<div class="flex items-center gap-3">
			<h2 class="min-w-0 flex-1 truncate text-lg font-semibold text-ink-gray-9">
				Run {{ runLabel }}
			</h2>
			<Badge
				variant="subtle"
				:theme="STATUS_THEME[run.status] || 'gray'"
				:label="run.status"
			/>
			<Button
				v-if="run.dashboard"
				variant="subtle"
				label="Open dashboard"
				iconLeft="bar-chart-2"
				@click="router.push('/dashboards/' + run.dashboard)"
			/>
			<Button
				v-if="run.conversation"
				variant="subtle"
				label="Open Chat"
				iconLeft="message-circle"
				@click="router.push('/c/' + run.conversation)"
			/>
		</div>
		<div class="mt-1 flex flex-wrap items-center gap-1.5 text-sm text-ink-gray-5">
			<span>{{ run.trigger || "manual" }}</span>
			<template v-if="run.started_at">
				<span>·</span>
				<Tooltip :text="exactDate(run.started_at)">
					<span>started {{ timeAgo(run.started_at) }}</span>
				</Tooltip>
			</template>
			<template v-if="run.finished_at">
				<span>·</span>
				<Tooltip :text="exactDate(run.finished_at)">
					<span>finished {{ timeAgo(run.finished_at) }}</span>
				</Tooltip>
			</template>
		</div>

		<!-- Scribe run: a Custom App Learning agent writes wiki pages, not findings.
		     Show the pages it wrote (with links) instead of a findings list, so a
		     successful run reads as "N pages written", never a failure / "0 findings". -->
		<template v-if="isScribe">
			<div
				v-if="run.status === 'failed'"
				class="mt-4 flex items-start gap-2 rounded-lg border border-outline-red-1 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
			>
				<FeatherIcon name="x-circle" class="mt-0.5 size-4 shrink-0" />
				<span>{{ run.error || "This run failed before writing any pages." }}</span>
			</div>
			<div
				v-else-if="run.coverage_note"
				class="mt-4 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
			>
				<FeatherIcon name="alert-triangle" class="mt-0.5 size-4 shrink-0" />
				<span>{{ coverageNote }}.</span>
			</div>

			<div class="mt-5 text-base font-medium text-ink-gray-9">
				{{ scribePages.length || run.pages_written || 0 }} wiki page{{
					(scribePages.length || run.pages_written || 0) === 1 ? "" : "s"
				}}
				written
			</div>
			<div v-if="scribePages.length" class="mt-2 divide-y overflow-hidden rounded-lg border">
				<a
					v-for="p in scribePages"
					:key="p.slug"
					:href="wikiUrl(p.slug)"
					target="_blank"
					rel="noopener"
					class="flex items-center gap-2 px-3 py-2.5 hover:bg-surface-gray-1"
				>
					<FeatherIcon name="book-open" class="size-4 shrink-0 text-ink-gray-5" />
					<span class="min-w-0 flex-1 truncate text-base text-ink-gray-8">
						{{ p.title || p.slug }}
					</span>
					<FeatherIcon name="external-link" class="size-3.5 shrink-0 text-ink-gray-5" />
				</a>
			</div>
			<div v-else-if="run.status === 'running'" class="mt-2 py-6 text-sm text-ink-gray-5">
				Run in progress - pages appear here as they are written.
			</div>
			<div v-else class="mt-2 py-6 text-sm text-ink-gray-5">
				No wiki pages were written this run.
			</div>
		</template>

		<template v-else>
			<!-- coverage-honesty banner: a truncated scan must NEVER read as all-clear -->
			<div
				v-if="coverageWarning"
				class="mt-4 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
			>
				<FeatherIcon name="alert-triangle" class="mt-0.5 size-4 shrink-0" />
				<span>
					Partial scan - {{ coverageNote }}. Treat gaps as unreviewed, not clean.
				</span>
			</div>

			<!-- failed run: surface the error; no findings snapshot was recorded -->
			<div
				v-if="run.status === 'failed'"
				class="mt-4 flex items-start gap-2 rounded-lg border border-outline-red-1 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
			>
				<FeatherIcon name="x-circle" class="mt-0.5 size-4 shrink-0" />
				<span>{{ run.error || "This run failed before recording findings." }}</span>
			</div>

			<!-- state-filter chips (all/open/acknowledged/resolved) -->
			<div class="mt-5 flex items-center gap-2">
				<Button
					v-for="c in STATE_CHIPS"
					:key="c.value"
					:label="c.label"
					:variant="stateFilter === c.value ? 'solid' : 'subtle'"
					@click="stateFilter = c.value"
				/>
			</div>

			<div v-if="loading && !rows.length" class="flex justify-center py-10">
				<JvSpinner />
			</div>
			<!-- persistent fetch-error state: a failed load must never read as "No findings" -->
			<div v-else-if="loadError && !rows.length" class="py-8 text-sm text-ink-red-4">
				{{ loadError }}
			</div>
			<div v-else-if="!rows.length" class="py-8 text-sm text-ink-gray-5">
				{{ emptyText }}
			</div>

			<!-- findings grouped by severity: blocker → warning → note -->
			<div v-for="group in groups" :key="group.severity" class="mt-5">
				<div class="flex items-center gap-2">
					<span class="text-base font-semibold text-ink-gray-9">
						{{ SEVERITY_LABEL[group.severity] }}
					</span>
					<!-- true server-side count (envelope severity_counts), not the loaded slice -->
					<span class="text-sm text-ink-gray-5">({{ groupCount(group) }})</span>
				</div>
				<div class="mt-2 divide-y overflow-hidden rounded-lg border">
					<div v-for="f in group.rows" :key="f.name">
						<!-- collapsed row (div, not button - it hosts the state select);
					     role/tabindex + enter/space keep it keyboard-operable -->
						<div
							role="button"
							tabindex="0"
							:aria-expanded="isExpanded(f.name)"
							class="flex w-full cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-surface-gray-1"
							@click="toggleExpand(f.name)"
							@keydown.enter.prevent="toggleExpand(f.name)"
							@keydown.space.prevent="toggleExpand(f.name)"
						>
							<FeatherIcon
								name="chevron-right"
								class="size-4 shrink-0 text-ink-gray-5 transition-all duration-300 ease-in-out"
								:class="{ 'rotate-90': isExpanded(f.name) }"
							/>
							<Badge
								class="shrink-0"
								variant="subtle"
								:theme="SEVERITY_THEME[f.severity] || 'gray'"
								:label="severityBadgeLabel(f.severity)"
							/>
							<span class="w-20 shrink-0 truncate font-mono text-sm text-ink-gray-5">
								{{ f.rule_id || "-" }}
							</span>
							<span class="min-w-0 flex-1 truncate text-base text-ink-gray-8">
								{{ f.title }}
							</span>
							<span
								v-if="f.amount != null && f.amount !== ''"
								class="shrink-0 text-right text-base text-ink-gray-8"
							>
								{{ fmtAmount(f.amount) }}
							</span>
							<Badge
								class="shrink-0"
								variant="subtle"
								:theme="RECURRENCE_THEME[f.recurrence] || 'gray'"
								:label="RECURRENCE_LABEL[f.recurrence] || 'New'"
							/>
							<!-- stop keydown too: enter/space on the select must not toggle the row -->
							<div class="w-36 shrink-0" @click.stop @keydown.stop>
								<FormControl
									type="select"
									:options="STATE_OPTIONS"
									:modelValue="f.state"
									:disabled="busy === f.name"
									@update:modelValue="(v) => moveFinding(f, v)"
								/>
							</div>
						</div>

						<!-- expanded: the recorded detail - detail_md, the referenced
					     document, the statutory caveat, and the finding actions -->
						<div
							v-if="isExpanded(f.name)"
							class="border-t bg-surface-gray-1 px-4 py-3"
						>
							<!-- O1: renderMarkdown from @/markdown (escapes HTML first - safe) -->
							<div
								v-if="f.detail_md"
								class="prose prose-sm max-w-none"
								v-html="renderMarkdown(f.detail_md)"
							/>
							<div v-else class="text-sm text-ink-gray-5">
								No further detail recorded.
							</div>

							<div
								v-if="f.ref_doctype && f.ref_name"
								class="mt-3 flex items-center gap-2 text-sm"
							>
								<span class="shrink-0 text-ink-gray-5">Reference</span>
								<a
									:href="refUrl(f)"
									target="_blank"
									rel="noopener"
									class="flex min-w-0 items-center gap-1 text-ink-gray-8 hover:underline"
								>
									<span class="truncate"
										>{{ f.ref_doctype }} {{ f.ref_name }}</span
									>
									<FeatherIcon
										name="external-link"
										class="size-3.5 shrink-0 text-ink-gray-5"
									/>
								</a>
							</div>

							<!-- statutory caveat: the recorded basis, never a fabricated fix -->
							<div
								v-if="f.section || f.effective_date || f.disclaimer"
								class="mt-3 rounded bg-surface-gray-2 px-3 py-2 text-xs text-ink-gray-5"
							>
								{{ caveatText(f) }}
							</div>

							<div class="mt-3 flex items-center gap-2">
								<Button
									variant="subtle"
									label="Discuss in chat"
									iconLeft="message-circle"
									:loading="chatBusy === f.name"
									:disabled="!!chatBusy && chatBusy !== f.name"
									@click="discussInChat(f)"
								/>
								<Button
									v-if="f.ref_doctype && f.ref_name"
									variant="subtle"
									label="Open document"
									iconLeft="external-link"
									@click="openDocument(f)"
								/>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- coverage honesty: the page cap must never be silent - always say how
		     much of the run is on screen, and offer the rest -->
			<div v-if="rows.length" class="mt-4 flex items-center justify-between gap-2">
				<Button
					v-if="hasMore"
					variant="subtle"
					label="Load more"
					:loading="loading"
					@click="loadMore()"
				/>
				<div v-else />
				<span class="text-sm text-ink-gray-5"
					>Showing {{ rows.length }} of {{ total }}</span
				>
			</div>
		</template>
	</div>
</template>

<script setup>
// FindingsPanel - the right pane of AgentRunsBoard (DESIGN-V3 §7.2): the
// selected run's findings, grouped by severity (blocker → warning → note),
// each row expandable to the recorded detail_md (markdown), the referenced
// document, and the statutory caveat (section/effective_date/disclaimer).
// A partial run always carries a coverage-honesty banner. Actions per finding:
// Discuss in chat (take_finding_to_chat → /c/:id), Open document, and the
// open/acknowledged/resolved state select → setFindingState (optimistic).
// No remediation text is ever fabricated - only what the run persisted.
import { ref, computed, watch } from "vue";
import { useRouter } from "vue-router";
import { Badge, Button, FeatherIcon, FormControl, Tooltip, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { timeAgo, exactDate, formatDate } from "@/utils/datetime";
import { renderMarkdown } from "@/markdown";
import * as api from "@/api";
import { takeFindingToChat } from "@/api/agents";

const props = defineProps({
	// full row from list_runs_page: {name, status, trigger, started_at,
	// finished_at, conversation, dashboard, findings_count, blocker_count,
	// error, coverage_note, nature, pages_written, pages_json, ...}. `nature`
	// + `pages_written`/`pages_json` drive the scribe branch (wiki pages instead
	// of findings); `dashboard` is the saved Jarvis Dashboard
	// name (Run.dashboard); the run header links to /dashboards/:id when set.
	// NB: list_runs_page must include `dashboard` in its fields for the link to
	// appear (cross-file — see report notes).
	run: { type: Object, required: true },
});

const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const STATUS_THEME = { running: "blue", completed: "green", partial: "orange", failed: "red" };
const SEVERITY_THEME = { blocker: "red", warning: "orange", note: "gray" };
const SEVERITY_ORDER = ["blocker", "warning", "note"];
const SEVERITY_LABEL = { blocker: "Blockers", warning: "Warnings", note: "Notes" };
const RECURRENCE_THEME = { new: "blue", recurring: "gray", resolved: "green" };
const RECURRENCE_LABEL = { new: "New", recurring: "Recurring", resolved: "Resolved" };
const STATE_CHIPS = [
	{ label: "All", value: "" },
	{ label: "Open", value: "open" },
	{ label: "Acknowledged", value: "acknowledged" },
	{ label: "Resolved", value: "resolved" },
];
const STATE_OPTIONS = [
	{ label: "Open", value: "open" },
	{ label: "Acknowledged", value: "acknowledged" },
	{ label: "Resolved", value: "resolved" },
];

const PAGE_LENGTH = 100;

const rows = ref([]);
const total = ref(0);
const hasMore = ref(false);
// true per-severity counts from the list_findings envelope ({blocker,
// warning, note}); null until an envelope-shaped response arrives
const severityCounts = ref(null);
const loading = ref(false);
const loadError = ref("");
const stateFilter = ref("");
const busy = ref("");
const chatBusy = ref("");
const expanded = ref(new Set());

const runLabel = computed(() =>
	props.run && props.run.started_at ? timeAgo(props.run.started_at) : props.run.name
);
// a failed run shows ONLY the red failed banner - never the amber partial one
const coverageWarning = computed(
	() =>
		props.run.status === "partial" ||
		(!!props.run.coverage_note && props.run.status !== "failed")
);
const coverageNote = computed(() => {
	const note = String(props.run.coverage_note || "").trim();
	// the sentence supplies its own terminal punctuation
	return note.replace(/[.\s]+$/, "") || "some records were not reviewed";
});

// Scribe runs (Custom App Learning) write wiki pages, not findings: render the
// pages tally + links instead of the findings machinery.
const isScribe = computed(() => props.run && props.run.nature === "Scribe");
const scribePages = computed(() => {
	try {
		const arr = JSON.parse(props.run.pages_json || "[]");
		return Array.isArray(arr) ? arr.filter((p) => p && p.slug) : [];
	} catch {
		return [];
	}
});
// The wiki page docname IS its slug (autoname field:slug), so the desk form
// opens the exact page an admin can read/edit.
function wikiUrl(slug) {
	return `/app/jarvis-wiki-page/${encodeURIComponent(slug)}`;
}
const emptyText = computed(() => {
	if (props.run.status === "running")
		return "Run in progress - findings appear when it completes.";
	if (props.run.status === "failed") return "This run recorded no findings.";
	return `No ${stateFilter.value ? stateFilter.value + " " : ""}findings for this run.`;
});

// blocker → warning → note, unknown severities folded into note
const groups = computed(() => {
	const by = {};
	for (const r of rows.value) {
		const sev = SEVERITY_ORDER.includes(r.severity) ? r.severity : "note";
		(by[sev] = by[sev] || []).push(r);
	}
	return SEVERITY_ORDER.filter((s) => by[s] && by[s].length).map((s) => ({
		severity: s,
		rows: by[s],
	}));
});

// header count = the TRUE server-side count for the severity (envelope
// severity_counts); the loaded slice length is only the legacy fallback
function groupCount(group) {
	const c = severityCounts.value && severityCounts.value[group.severity];
	return c != null ? c : group.rows.length;
}

// title-case badge label (Blocker/Warning/Note) to match the group headers
const SEVERITY_BADGE = { blocker: "Blocker", warning: "Warning", note: "Note" };
function severityBadgeLabel(sev) {
	if (SEVERITY_BADGE[sev]) return SEVERITY_BADGE[sev];
	const s = String(sev || "note");
	return s.charAt(0).toUpperCase() + s.slice(1);
}

// monotonic request id - rapid rail clicks must not land stale findings
let reqId = 0;
async function load({ append = false } = {}) {
	if (!props.run || !props.run.name) return;
	// a scribe run has no findings snapshot to fetch — its pages come inline
	if (isScribe.value) return;
	const id = ++reqId;
	loading.value = true;
	try {
		const res = await api.listAgentFindings({
			run: props.run.name,
			state: stateFilter.value || undefined,
			start: append ? rows.value.length : 0,
			page_length: PAGE_LENGTH,
		});
		if (id !== reqId) return;
		// envelope {rows, total, has_more, start, page_length, severity_counts};
		// defensively fall back to the legacy bare-array shape
		const isEnvelope = !!(res && !Array.isArray(res) && Array.isArray(res.rows));
		const page = (isEnvelope ? res.rows : res) || [];
		if (append) {
			const seen = new Set(rows.value.map((r) => r.name));
			rows.value = [...rows.value, ...page.filter((r) => !seen.has(r.name))];
		} else {
			rows.value = page;
		}
		if (isEnvelope) {
			total.value = res.total != null ? res.total : rows.value.length;
			hasMore.value = !!res.has_more;
			severityCounts.value = res.severity_counts || null;
		} else {
			total.value = rows.value.length;
			hasMore.value = false;
			severityCounts.value = null;
		}
		loadError.value = "";
	} catch (e) {
		if (id === reqId) {
			loadError.value = errMsg(e);
			toast.error(loadError.value);
		}
	} finally {
		if (id === reqId) loading.value = false;
	}
}
function loadMore() {
	if (!loading.value && hasMore.value) load({ append: true });
}

// reload on run switch AND on status flip (a re-pinned running run that just
// completed now has a findings snapshot to show). Watch the name/status
// STRINGS, not the run object - the rail re-pins a fresh row object on every
// refresh and object identity alone must not re-fetch findings.
watch(
	[() => props.run && props.run.name, () => props.run && props.run.status],
	([name], [prevName] = []) => {
		if (name !== prevName) {
			rows.value = [];
			total.value = 0;
			hasMore.value = false;
			severityCounts.value = null;
			loadError.value = "";
			expanded.value = new Set();
			if (stateFilter.value) {
				stateFilter.value = ""; // its watcher runs the (re)load
				return;
			}
		}
		load();
	},
	{ immediate: true }
);
watch(stateFilter, () => load());

function isExpanded(name) {
	return expanded.value.has(name);
}
function toggleExpand(name) {
	const next = new Set(expanded.value);
	if (next.has(name)) next.delete(name);
	else next.add(name);
	expanded.value = next;
}

// ── triage: state select → setFindingState (optimistic, revert on error) ────
async function moveFinding(f, state) {
	if (!state || state === f.state || busy.value) return;
	const prev = f.state;
	f.state = state; // optimistic
	busy.value = f.name;
	try {
		await api.setFindingState(f.name, state);
		toast.success(`Finding ${state}`);
		// reconcile with the active state-filter chip: a finding moved OUT of
		// the filtered state leaves the visible list (and its counts) at once -
		// acknowledging while viewing "Open" must not leave a stale row
		if (stateFilter.value && state !== stateFilter.value) {
			rows.value = rows.value.filter((r) => r.name !== f.name);
			total.value = Math.max(0, total.value - 1);
			const sev = SEVERITY_ORDER.includes(f.severity) ? f.severity : "note";
			if (severityCounts.value && severityCounts.value[sev] != null) {
				severityCounts.value = {
					...severityCounts.value,
					[sev]: Math.max(0, severityCounts.value[sev] - 1),
				};
			}
			if (expanded.value.has(f.name)) {
				const next = new Set(expanded.value);
				next.delete(f.name);
				expanded.value = next;
			}
		}
	} catch (e) {
		f.state = prev;
		toast.error(errMsg(e));
	} finally {
		busy.value = "";
	}
}

// ── actions: take-to-chat + open-doc (decision: NO fabricated remediation) ──
async function discussInChat(f) {
	if (chatBusy.value) return;
	chatBusy.value = f.name;
	try {
		const res = (await takeFindingToChat(f.name)) || {};
		if (!res.conversation) {
			throw new Error(res.reason || "Could not open a conversation for this finding.");
		}
		router.push("/c/" + res.conversation);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		chatBusy.value = "";
	}
}
function openDocument(f) {
	if (f.ref_doctype && f.ref_name) window.open(refUrl(f), "_blank");
}

function refUrl(row) {
	const dt = String(row.ref_doctype || "")
		.toLowerCase()
		.replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(row.ref_name)}`;
}
// "Statutory basis: {section} (effective {date}). {disclaimer}" - only the
// pieces the run recorded
function caveatText(f) {
	const bits = [];
	if (f.section) {
		const eff = f.effective_date ? ` (effective ${formatDate(f.effective_date)})` : "";
		bits.push(`Statutory basis: ${f.section}${eff}.`);
	} else if (f.effective_date) {
		bits.push(`Effective ${formatDate(f.effective_date)}.`);
	}
	if (f.disclaimer) bits.push(String(f.disclaimer));
	return bits.join(" ");
}
function fmtAmount(v) {
	const n = Number(v);
	return isNaN(n) ? String(v) : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
</script>
