<template>
	<div class="flex h-full flex-col gap-3">
		<!-- toolbar: search + kind/status quick filters (ListPage idiom, hand-
		     rolled per this view's own fetch loop - see script comment) -->
		<div class="flex flex-wrap items-center gap-2">
			<FormControl
				type="text"
				class="min-w-0 flex-1"
				placeholder="Search notes"
				:modelValue="search"
				@update:modelValue="(v) => (search = v)"
			>
				<template #prefix>
					<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
				</template>
			</FormControl>
			<FormControl
				type="select"
				class="w-32 shrink-0"
				:options="KIND_OPTIONS"
				:modelValue="kindFilter"
				@update:modelValue="(v) => (kindFilter = v)"
			/>
			<FormControl
				type="select"
				class="w-36 shrink-0"
				:options="STATUS_OPTIONS"
				:modelValue="statusFilter"
				@update:modelValue="(v) => (statusFilter = v)"
			/>
		</div>

		<!-- list -->
		<div v-if="notes.loading && !notes.rows.length" class="py-8 text-center">
			<LoadingIndicator class="size-5 text-ink-gray-5" />
		</div>
		<div
			v-else-if="!notes.rows.length && filtered"
			class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-10 text-center"
		>
			<FeatherIcon name="search" class="size-6 text-ink-gray-5" />
			<span class="mt-1 text-base font-medium text-ink-gray-8">No matching notes</span>
			<span class="text-p-base text-ink-gray-6">Try a different search or filter.</span>
		</div>
		<div
			v-else-if="!notes.rows.length"
			class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-10 text-center"
		>
			<FeatherIcon name="edit-3" class="size-6 text-ink-gray-5" />
			<span class="mt-1 text-base font-medium text-ink-gray-8">No notes yet</span>
			<span class="text-p-base text-ink-gray-6">
				Answers and anything you tell {{ agentName }} land here.
			</span>
		</div>
		<div v-else class="divide-y rounded-lg border">
			<div
				v-for="row in notes.rows"
				:key="row.name"
				class="flex cursor-pointer items-start justify-between gap-3 px-4 py-3 hover:bg-surface-gray-1"
				@click="openDetail(row)"
			>
				<div class="min-w-0 flex-1">
					<div class="flex flex-wrap items-center gap-2">
						<Badge
							:theme="kindBadge(row.kind).theme"
							:variant="kindBadge(row.kind).variant"
							size="sm"
							:label="row.kind"
						/>
						<span
							v-if="row.question"
							class="inline-flex items-center gap-1 text-sm text-ink-gray-5"
						>
							<FeatherIcon name="help-circle" class="size-3.5" />
							Answers a question
						</span>
					</div>
					<p class="mt-1.5 line-clamp-2 text-sm text-ink-gray-8">
						{{ row.excerpt || row.transcript || "(no text)" }}
					</p>
					<div class="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-gray-5">
						<Badge
							variant="subtle"
							:theme="statusBadge(row.status).theme"
							size="sm"
							:label="statusBadge(row.status).label"
						/>
						<Tooltip :text="exactDate(row.creation)">
							<span>{{ timeAgo(row.creation) }}</span>
						</Tooltip>
					</div>
				</div>
			</div>
		</div>

		<!-- footer: "N of M" + Load more (ListPage's ListFooter idiom, hand-rolled -
		     see design-language.md §6/§10 on the upstream ListFooter Button bug) -->
		<div
			v-if="notes.rows.length"
			class="flex items-center justify-between text-sm text-ink-gray-5"
		>
			<span>{{ notes.rows.length }} of {{ notes.total }}</span>
			<Button
				v-if="notes.hasMore"
				variant="subtle"
				label="Load more"
				:loading="notes.loading"
				@click="fetchNotes('more')"
			/>
		</div>

		<NoteDetailModal
			v-model="modalOpen"
			:name="selectedName"
			@changed="onNoteChanged"
			@reanswer="(questionName) => emit('reanswer', questionName)"
		/>
	</div>
</template>

<script setup>
// NotesView - the Notes sub-tab body inside PersonaliseTab (DESIGN.md
// sections 5/6b, Wave F3). Self-contained: fetches its own page via
// listNotesPage, owns its own toolbar state, and opens NoteDetailModal on row
// click. Pagination follows the HAND-ROLLED reqId-guarded fetch idiom already
// established by components/business/NotesPane.vue:238-278 (this view's
// direct precedent) rather than the generic useListPage()/ListPage.vue frame
// - kind/status/search here are flat kwargs straight to listNotesPage, not a
// filters-envelope, so the hand-rolled loop is the better fit (matches
// research/frontend.md §9.2's own note that NotesPane/ReviewTab already do
// this for the same reason).
//
// Parent contract (PersonaliseTab, built by F2): mount this as the Notes
// sub-tab's body with no required props. Listen for `@reanswer` and switch
// the nested TabBar to the Questions sub-tab, selecting the question named by
// the emitted payload (see NoteDetailModal.vue's own comment for the full
// emit contract - this component only passes it through unchanged).
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import {
	Badge,
	Button,
	FeatherIcon,
	FormControl,
	LoadingIndicator,
	Tooltip,
	toast,
} from "frappe-ui";
import { timeAgo, exactDate } from "@/utils/datetime";
import { listNotesPage } from "@/api/personalise";
import NoteDetailModal from "./NoteDetailModal.vue";
import { agentName } from "@/branding";

const emit = defineEmits(["reanswer"]);

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const KIND_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Text", value: "Text" },
	{ label: "Voice", value: "Voice" },
	{ label: "Attachment", value: "Attachment" },
	{ label: "Link", value: "Link" },
];
const STATUS_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "New", value: "New" },
	{ label: "Processed", value: "Processed" },
	{ label: "Archived", value: "Archived" },
];

// Kind badge theme map (design-language.md §5's frozen spec: Text gray,
// Voice blue, Attachment orange, Link blue OUTLINE - the only kind that
// isn't `subtle`, so Link reads visually distinct from Voice at a glance).
const KIND_BADGE = {
	Text: { theme: "gray", variant: "subtle" },
	Voice: { theme: "blue", variant: "subtle" },
	Attachment: { theme: "orange", variant: "subtle" },
	Link: { theme: "blue", variant: "outline" },
};
function kindBadge(kind) {
	return KIND_BADGE[kind] || { theme: "gray", variant: "subtle" };
}

// Status badge copy is the user-facing receipt language (DESIGN.md §5c/§6:
// "two-stage receipts"), not the raw doctype status word.
const STATUS_BADGE = {
	New: { theme: "blue", label: "Processing soon" },
	Processed: { theme: "green", label: "In your wiki" },
	Archived: { theme: "gray", label: "Archived" },
};
function statusBadge(status) {
	return STATUS_BADGE[status] || { theme: "gray", label: status || "" };
}

// ── state ────────────────────────────────────────────────────────────────────
const search = ref("");
const kindFilter = ref("");
const statusFilter = ref("");
const notes = reactive({ rows: [], total: 0, hasMore: false, loading: false });
const modalOpen = ref(false);
const selectedName = ref("");

const filtered = computed(() => !!(search.value.trim() || kindFilter.value || statusFilter.value));

// ── loader (NotesPane.vue:238-278 idiom, verbatim) ────────────────────────────
let reqId = 0;

async function fetchNotes(mode = "reset") {
	const id = ++reqId;
	const append = mode === "more";
	notes.loading = true;
	try {
		const res = await listNotesPage({
			start: append ? notes.rows.length : 0,
			page_length: 20,
			kind: kindFilter.value || undefined,
			status: statusFilter.value || undefined,
			search: search.value.trim() || undefined,
		});
		if (id !== reqId) return; // stale - a newer request superseded this one
		const rows = res.rows || [];
		notes.rows = append ? [...notes.rows, ...rows] : rows;
		notes.total = res.total || 0;
		notes.hasMore = !!res.has_more;
	} catch (e) {
		if (id !== reqId) return;
		toast.error(errMsg(e));
	} finally {
		if (id === reqId) notes.loading = false;
	}
}

function reload() {
	return fetchNotes("reset");
}
defineExpose({ reload });

// search debounced 300ms; kind/status quick-filters (selects) reset immediately
let searchTimer = null;
watch(search, () => {
	clearTimeout(searchTimer);
	searchTimer = setTimeout(() => fetchNotes("reset"), 300);
});
watch(kindFilter, () => fetchNotes("reset"));
watch(statusFilter, () => fetchNotes("reset"));
onBeforeUnmount(() => clearTimeout(searchTimer));

// ── detail modal ─────────────────────────────────────────────────────────────
function openDetail(row) {
	selectedName.value = row.name;
	modalOpen.value = true;
}

function onNoteChanged() {
	// Deletion (currently the only mutation the modal reports) - refetch so
	// the row list matches server state; kind/status filters stay as-is.
	fetchNotes("reset");
}

// ── init ─────────────────────────────────────────────────────────────────────
onMounted(() => fetchNotes("reset"));
</script>
