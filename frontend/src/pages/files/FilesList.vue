<template>
	<div
		class="relative flex h-full flex-col overflow-hidden"
		@dragenter.prevent="onDragEnter"
		@dragover.prevent
		@dragleave="onDragLeave"
		@drop.prevent="onDrop"
	>
		<ListPage
			:breadcrumbs="[{ label: 'File Box', route: { name: 'FilesList' } }]"
			:columns="columns"
			:rows="rows"
			:loading="loading"
			:error="error"
			:total="total"
			:has-more="hasMore"
			:quick-filters="quickFilters"
			:filter-defs="filterDefs"
			:filters="filters"
			:sort-options="sortOptions"
			:sort="sort"
			:page-length="pageLength"
			:default-sort="DEFAULT_SORT"
			:selectable="true"
			:on-row-click="openChat"
			storage-key="files"
			:empty-state="{
				icon: 'inbox',
				title: 'No files yet',
				description: `Add or drop a document and ${agentName} will process it.`,
			}"
			@update:filters="onFiltersUpdate"
			@update:sort="(s) => setSort(s.field, s.dir)"
			@update:page-length="(v) => (pageLength = v)"
			@load-more="loadMore"
			@refresh="resetLoad"
		>
			<template #right-header>
				<Button label="Clear Processed" @click="clearProcessed" />
				<Button
					variant="solid"
					label="Add Files"
					iconLeft="upload-cloud"
					@click="pickFiles"
				/>
			</template>

			<!-- persistent drop card: click → picker, drag anywhere on the page →
			     highlight (the root handlers drive `dragging`), drop → uploadBatch -->
			<template #banner>
				<div class="mb-3">
					<!-- optional extra skill, applied ALONGSIDE the system's chosen skill,
					     to the files dropped next -->
					<div class="mb-2 flex items-center gap-2">
						<span class="shrink-0 text-p-sm text-ink-gray-6">Also apply skill</span>
						<Autocomplete
							class="w-72"
							:options="skillOptions"
							:modelValue="pinnedSkill"
							placeholder="None (system default)"
							@update:modelValue="(o) => (pinnedSkill = o ? o.value : '')"
						/>
					</div>
					<div
						role="button"
						tabindex="0"
						class="flex cursor-pointer flex-col items-center gap-1.5 rounded-lg border-2 border-dashed px-5 py-5 text-center transition-colors"
						:class="
							dragging
								? 'border-outline-gray-4 bg-surface-gray-2'
								: 'border-outline-gray-2 bg-surface-white hover:bg-surface-gray-1'
						"
						@click="pickFiles"
						@keydown.enter.prevent="pickFiles"
						@keydown.space.prevent="pickFiles"
					>
						<FeatherIcon name="upload-cloud" class="size-6 text-ink-gray-5" />
						<div class="text-base font-medium text-ink-gray-8">
							{{
								dragging
									? "Drop files to add to File Box"
									: "Drop files here, or click to browse"
							}}<span v-if="uploadingCount"> - {{ uploadingCount }} uploading…</span>
						</div>
						<div class="max-w-2xl text-p-sm text-ink-gray-6">
							Drop your files - single or in bulk - and leave them.
							{{ agentName }} identifies each file's nature, processes it in the
							background and shows what happened on each row - the draft it created,
							or why it couldn't. If it needs your input, it asks in the
							<!-- .stop keeps the link from also triggering the card's pickFiles
							     (click) and from having Enter swallowed by the card's
							     keydown.enter.prevent -->
							<router-link
								to="/approvals"
								class="font-semibold text-ink-blue-3 hover:underline"
								@click.stop
								@keydown.enter.stop
								>Approval Board</router-link
							>
							- watch for the <span class="font-semibold">red dot</span> there and
							answer its questions.
						</div>
					</div>
					<!-- per-file error chips (last 8, dismissible) -->
					<div v-if="dropErrors.length" class="mt-2 flex flex-wrap gap-1.5">
						<div
							v-for="d in dropErrors"
							:key="d.key"
							class="flex h-6 min-w-0 items-center gap-1.5 rounded border border-outline-red-1 bg-surface-red-1 px-2 text-sm text-ink-red-4"
						>
							<FeatherIcon name="alert-circle" class="size-3.5 shrink-0" />
							<span class="max-w-[280px] truncate" :title="`${d.name}: ${d.error}`">
								{{ d.name }}: {{ d.error }}
							</span>
							<Button
								variant="ghost"
								icon="x"
								label="Dismiss"
								class="!h-4 !w-4"
								@click="dismissDropError(d.key)"
							/>
						</div>
					</div>
				</div>
			</template>

			<template #cell-title="{ row }">
				<div class="min-w-0">
					<div class="flex items-center gap-2 overflow-hidden">
						<FeatherIcon name="file-text" class="size-4 shrink-0 text-ink-gray-5" />
						<div class="truncate text-base font-medium text-ink-gray-9">
							{{ stripTitle(row.title) }}
						</div>
					</div>
					<!-- the skill the owner pinned at drop time (owner-only from the API) -->
					<div v-if="row.pinned_skill" class="mt-0.5 pl-6">
						<Tooltip
							:text="`Tagged skill: ${pinnedLabel(row.pinned_skill, customSkills)}`"
						>
							<Badge
								variant="subtle"
								theme="gray"
								:label="`tagged: ${pinnedLabel(row.pinned_skill, customSkills)}`"
								class="max-w-[16rem] truncate"
							/>
						</Tooltip>
					</div>
				</div>
			</template>

			<template #cell-status="{ row }">
				<Badge
					variant="subtle"
					:theme="statusBadge(row).theme"
					:label="statusBadge(row).label"
				/>
			</template>

			<!-- one-line result; links open the approval (in-app) or the draft
			     (Desk, new tab) without also opening the row's chat -->
			<template #cell-result="{ row }">
				<div class="min-w-0 truncate text-base text-ink-gray-7" :title="row.result || ''">
					<a
						v-if="resultLink(row) && resultLink(row).kind === 'desk'"
						:href="resultLink(row).href"
						target="_blank"
						rel="noopener noreferrer"
						class="text-ink-blue-3 hover:underline"
						@click.stop
						>{{ row.result }}</a
					>
					<router-link
						v-else-if="resultLink(row)"
						:to="resultLink(row).href"
						class="text-ink-blue-3 hover:underline"
						@click.stop
						>{{ row.result }}</router-link
					>
					<span v-else>{{ row.result }}</span>
				</div>
			</template>

			<template #cell-creation="{ row }">
				<div class="flex w-full items-center justify-end">
					<Tooltip :text="exactDate(row.creation)">
						<div class="truncate text-base">{{ timeAgo(row.creation) }}</div>
					</Tooltip>
				</div>
			</template>

			<template #cell-_rerun="{ row }">
				<div class="flex w-full items-center justify-end" @click.stop.prevent>
					<Button
						v-if="canRerun(row)"
						variant="ghost"
						icon="rotate-ccw"
						size="sm"
						:loading="rerunningRow === row.name"
						:disabled="!!rerunningRow"
						label="Re-run"
						:tooltip="'Re-run'"
						@click="rerunRow(row)"
					/>
				</div>
			</template>

			<template #cell-_preview="{ row }">
				<div class="flex w-full items-center justify-end" @click.stop.prevent>
					<Button
						v-if="row.file_url"
						variant="ghost"
						icon="eye"
						size="sm"
						label="Preview file"
						:tooltip="'Preview'"
						@click="openPreview(row)"
					/>
				</div>
			</template>

			<template #select-actions="{ selections, unselectAll }">
				<Dropdown
					:options="[
						{ label: 'Re-run', onClick: () => bulkRerun(selections, unselectAll) },
						{ label: 'Delete', onClick: () => bulkDelete(selections, unselectAll) },
					]"
				>
					<Button icon="more-horizontal" variant="ghost" label="Bulk actions" />
				</Dropdown>
			</template>
		</ListPage>

		<input ref="fileInput" type="file" multiple class="hidden" @change="onPick" />

		<FilePreview
			v-model="preview.show"
			:file-url="preview.url"
			:file-name="preview.name"
			:file-type="preview.type"
		/>
	</div>
</template>

<script setup>
// File Box list - DESIGN-V3 §5.7 + §15.1: search quick filter (envelope
// `search`), persistent drop card (click → picker, page-wide drag highlights
// it, drop anywhere uploads) with per-file error chips, status quick filter
// with ?status= deep link, date-range filter, processing/applying poll (5s) +
// visibilitychange refresh, per-row file preview (FilePreview dialog), bulk
// delete with skip reasons, Clear Processed.
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
	Button,
	Badge,
	FeatherIcon,
	Tooltip,
	Dropdown,
	Autocomplete,
	toast,
	confirmDialog,
} from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import FilePreview from "@/components/FilePreview.vue";
import { useListPage } from "@/composables/useListPage";
import { useShellStore } from "@/stores/shell";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as api from "@/api";
import { agentName } from "@/branding";
import { errMessage as errMsg, errHtml, escapeHtml } from "@/lib/errors";
import { skillOptions as buildSkillOptions, pinnedLabel } from "@/lib/fileboxSkills";
import {
	STATUSES,
	STATUS_OPTIONS,
	statusBadge,
	resultLink,
	isLive,
	canRerun,
	bulkRerunToast,
} from "@/lib/fileboxStatus";

const route = useRoute();
const router = useRouter();
const store = useShellStore();

// ── list config ──────────────────────────────────────────────────────────────
// statuses, badges and result links live in @/lib/fileboxStatus (tested).
const columns = [
	{ label: "File", key: "title", width: 3 },
	{ label: "Status", key: "status", width: "10rem" },
	{ label: "Result", key: "result", width: 3 },
	{ label: "Added", key: "creation", width: "8rem", align: "right" },
	{ label: "", key: "_rerun", width: "3rem", align: "right" },
	{ label: "", key: "_preview", width: "3rem", align: "right" },
];

// ── drop-time skill pin ──────────────────────────────────────────────────────
// Optionally pin ONE processing skill for the files dropped now (else the agent
// auto-discovers one). Option/label logic lives in @/lib/fileboxSkills (tested).
const pinnedSkill = ref(""); // "" = Auto (no pin)
const customSkills = ref([]);
const skillOptions = computed(() => buildSkillOptions(customSkills.value));
async function loadSkills() {
	// best-effort: if listing fails, Auto + OCR (hardcoded) stay selectable
	try {
		customSkills.value = ((await api.listCustomSkills()) || []).filter((s) => s.enabled);
	} catch (e) {
		customSkills.value = [];
	}
}
// search rides the quick-filter strip (§15.1): it lives in the filters object
// so the input stays controlled, and fetchFn moves it onto the envelope's
// `search` param (backend matches the title).
const quickFilters = [
	{ key: "search", label: "Search files", type: "text" },
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
];
const filterDefs = [
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
	{ key: "daterange", label: "Added", type: "daterange" },
];
const sortOptions = [
	{ label: "Added", value: "creation" },
	{ label: "File", value: "title" },
];
const DEFAULT_SORT = { field: "creation", dir: "desc" };

// status deep link: seed the filter from ?status= (validated set, parity)
const initialStatus = STATUSES.includes(route.query.status) ? route.query.status : "";

const {
	rows,
	total,
	hasMore,
	loading,
	error,
	filters,
	setFilters,
	sort,
	setSort,
	pageLength,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: (p) => {
		// the backend whitelists filter keys and throws on "search" - strip it
		// out of filters and send it as the envelope's search param instead
		const { search: q, ...rest } = p.filters || {};
		return api.fileboxListPage({ ...p, search: q || p.search || "", filters: rest });
	},
	defaultSort: DEFAULT_SORT,
	storageKey: "files",
	initialFilters: initialStatus ? { status: initialStatus } : {},
});

function onFiltersUpdate(next) {
	setFilters(next);
	syncStatusQuery();
}
// keep ?status= in the URL so breadcrumb returns preserve the filter (D32)
function syncStatusQuery() {
	const q = { ...route.query };
	if (filters.status) q.status = filters.status;
	else delete q.status;
	router.replace({ query: q });
}

function openChat(row) {
	router.push("/c/" + row.name);
}

// ── per-row file preview (FilePreview dialog) ────────────────────────────────
// rows carry file_url / file_name / file_type from list_inbound_page; rows
// without a file_url (e.g. legacy docs) simply don't render the eye button.
const preview = ref({ show: false, url: "", name: "", type: "" });
function openPreview(row) {
	if (!row.file_url) return;
	preview.value = {
		show: true,
		url: row.file_url,
		name: row.file_name || stripTitle(row.title),
		type: row.file_type || "",
	};
}

// ── upload (picker + drag-drop) ──────────────────────────────────────────────
const fileInput = ref(null);
function pickFiles() {
	fileInput.value && fileInput.value.click();
}
function onPick(ev) {
	uploadBatch(ev.target.files);
	ev.target.value = "";
}

// drop-card state: in-flight count ("{n} uploading…") + per-file error chips
const uploadingCount = ref(0);
const dropErrors = ref([]); // [{key, name, error}] - last 8, dismissible
let dropErrKey = 0;
function pushDropError(name, error) {
	dropErrors.value = [...dropErrors.value, { key: ++dropErrKey, name, error }].slice(-8);
}
function dismissDropError(key) {
	dropErrors.value = dropErrors.value.filter((d) => d.key !== key);
}

// One check per batch: a tagged skill File Box can no longer use keeps every file
// un-dropped (one toast), instead of one "skill not available" question per file.
// A failed check lets the drop go ahead: drop_file validates the pin itself.
async function pinUsable(pin) {
	let res;
	try {
		res = await api.fileboxCheckSkill(pin);
	} catch {
		return true;
	}
	if (res && res.available) return true;
	toast.error(
		escapeHtml(
			`The skill “${pin}” is no longer available for File Box, so nothing was added. ` +
				"Choose another skill (or None) and add the files again."
		)
	);
	pinnedSkill.value = "";
	loadSkills();
	return false;
}

async function uploadBatch(fileList) {
	const files = Array.from(fileList || []);
	if (!files.length) return;
	const pin = pinnedSkill.value;
	if (pin && !(await pinUsable(pin))) return;
	const failures = [];
	let okCount = 0;
	let waiting = 0; // dropped, but waiting on a skill question (the pin went away mid-batch)
	uploadingCount.value += files.length;
	const run = (async () => {
		await Promise.all(
			files.map(async (file) => {
				try {
					const up = await api.uploadFile(file);
					const res = await api.fileboxDrop(
						up.file_url,
						up.file_name,
						pin || undefined,
						up.name
					);
					if (!res || !res.ok) throw new Error((res && res.reason) || "drop failed");
					okCount++;
					if (res.needs_approval) waiting++;
				} catch (e) {
					failures.push({ name: file.name, error: errMsg(e) });
				} finally {
					uploadingCount.value = Math.max(0, uploadingCount.value - 1);
				}
			})
		);
		if (!okCount) throw new Error("upload failed");
		return okCount;
	})();
	try {
		await toast.promise(run, {
			loading: `Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`,
			success: (n) =>
				`Added ${n} file${n === 1 ? "" : "s"} to File Box` +
				(pin ? ` · tagged: ${pinnedLabel(pin, customSkills.value)}` : "") +
				(waiting ? ` · ${waiting} waiting for your choice` : ""),
			error: () => "Upload failed",
		});
	} catch (e) {
		// every file failed - the per-file chips below carry the reasons
	}
	for (const f of failures) pushDropError(f.name, f.error);
	if (okCount) resetLoad();
	if (waiting) store.refreshApprovalsCount();
}

// ── page-wide drag state (highlights the drop card; drop anywhere uploads) ───
const dragDepth = ref(0);
const dragging = computed(() => dragDepth.value > 0);
function hasFiles(ev) {
	const types = (ev.dataTransfer && ev.dataTransfer.types) || [];
	return Array.from(types).includes("Files");
}
function onDragEnter(ev) {
	if (hasFiles(ev)) dragDepth.value++;
}
function onDragLeave() {
	dragDepth.value = Math.max(0, dragDepth.value - 1);
}
function onDrop(ev) {
	dragDepth.value = 0;
	uploadBatch(ev.dataTransfer && ev.dataTransfer.files);
}

// ── inline Re-run (failed / no_draft only, AC8: never a second draft) ────────
const rerunningRow = ref("");
async function rerunRow(row) {
	if (rerunningRow.value || !canRerun(row)) return;
	rerunningRow.value = row.name;
	try {
		const res = await api.fileboxRerun(row.name);
		if (res && res.ok === false)
			toast.error(escapeHtml(res.reason || "Couldn't re-run this file"));
		else if (res && res.needs_approval) {
			// its tagged skill is gone: it waits on a question (the row's own words)
			const words = res.result || "Waiting for your choice on the Approval Board";
			toast.create({ type: "info", message: escapeHtml(words) });
			store.refreshApprovalsCount();
		} else toast.success("Re-running…");
		resetLoad();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		rerunningRow.value = "";
	}
}

function bulkRerun(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length) return;
	(async () => {
		try {
			const res = await api.fileboxRerunBulk(names);
			toast.create(bulkRerunToast(res, names.length));
			if (res && res.needs_choice) store.refreshApprovalsCount();
			unselectAll();
			resetLoad();
		} catch (e) {
			toast.error(errHtml(e));
		}
	})();
}

// ── bulk delete + clear processed ────────────────────────────────────────────
function bulkDelete(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length) return;
	confirmDialog({
		title: `Delete ${names.length} document${names.length === 1 ? "" : "s"}?`,
		message:
			"Deletes the conversations, their messages, the uploaded files, and their approval requests.",
		onConfirm: async ({ hideDialog }) => {
			try {
				const res = (await api.fileboxDeleteBulk(names)) || {};
				const skipped = res.skipped || [];
				const deleted = res.deleted != null ? res.deleted : names.length - skipped.length;
				if (skipped.length) {
					const reasons = [...new Set(skipped.map((s) => s.reason || "skipped"))].join(
						", "
					);
					toast.create({
						message: `${deleted} deleted · ${skipped.length} skipped (${reasons})`,
						type: "info",
					});
				} else {
					toast.success(`${deleted} document${deleted === 1 ? "" : "s"} deleted`);
				}
				unselectAll();
				hideDialog();
				resetLoad();
				store.refreshApprovalsCount();
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

function clearProcessed() {
	confirmDialog({
		title: "Clear processed documents?",
		message:
			"Deletes every document marked Draft created or No draft (with its file, messages, and approvals). Failed, processing, and needs-approval documents, and files with wiki notes awaiting review, are kept. The drafts themselves are not touched.",
		onConfirm: async ({ hideDialog }) => {
			try {
				const res = (await api.fileboxClearProcessed()) || {};
				toast.success(
					`${res.deleted || 0} document${(res.deleted || 0) === 1 ? "" : "s"} cleared`
				);
				hideDialog();
				resetLoad();
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

// ── freshness: poll while a row is processing/applying + refetch on visible ──
let pollTimer = null;
function onVisibility() {
	if (document.visibilityState === "visible") refreshKeep();
}
onMounted(() => {
	loadSkills();
	// cheap tick: only refetches when such a row is on screen
	pollTimer = setInterval(() => {
		if (rows.value.some(isLive)) refreshKeep();
	}, 5000);
	document.addEventListener("visibilitychange", onVisibility);
});
onBeforeUnmount(() => {
	if (pollTimer) clearInterval(pollTimer);
	document.removeEventListener("visibilitychange", onVisibility);
});

// ── cell helpers ─────────────────────────────────────────────────────────────
function stripTitle(title) {
	return (title || "").replace(/^File: /, "");
}
</script>
