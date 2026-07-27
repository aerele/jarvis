<template>
	<div class="flex h-full flex-col overflow-hidden">
		<ListPage
			:breadcrumbs="[{ label: 'Skills', route: { name: 'SkillsList' } }, { label: 'Wiki' }]"
			:columns="columns"
			:rows="rows"
			row-key="name"
			:loading="loading"
			:error="error"
			:total="total"
			:has-more="hasMore"
			:quick-filters="quickFilters"
			:filter-defs="filterDefs"
			:filters="filters"
			:page-length="pageLength"
			:on-row-click="openRow"
			storage-key="wiki"
			:empty-state="emptyState"
			@update:filters="setFilters"
			@update:page-length="(v) => (pageLength = v)"
			@load-more="loadMore"
			@refresh="resetLoad"
		>
			<template #right-header>
				<!-- SM extras: knowledge language + mirror sync + health check -->
				<Popover v-if="caps.is_sm" placement="bottom-end">
					<template #target="{ togglePopover }">
						<Button
							icon="settings"
							:tooltip="'Wiki settings'"
							@click="togglePopover()"
						/>
					</template>
					<template #body>
						<div
							class="my-2 w-[320px] rounded-lg bg-surface-modal p-3 shadow-2xl ring-1 ring-black ring-opacity-5"
						>
							<FormControl
								type="select"
								label="Knowledge language"
								:options="LANGUAGE_OPTIONS"
								:modelValue="caps.knowledge_language"
								@update:modelValue="changeLanguage"
							/>
							<p class="mt-1 text-p-sm text-ink-gray-5">
								Applies org-wide: wiki pages, extracted business facts and
								learned-skill drafts. English translates non-English input;
								Original keeps the source's language.
							</p>
							<div class="mt-3 flex flex-col gap-2 border-t pt-3">
								<Button
									variant="subtle"
									label="Sync to agent now"
									iconLeft="upload-cloud"
									:loading="syncing"
									@click="confirmSync"
								/>
								<p class="text-p-sm text-ink-gray-5">
									<template v-if="caps.wiki_mirror_last_synced_at">
										Last synced {{ timeAgo(caps.wiki_mirror_last_synced_at)
										}}<span v-if="caps.wiki_mirror_last_sync_status">
											- {{ caps.wiki_mirror_last_sync_status }}</span
										>
									</template>
									<template v-else>Not synced yet.</template>
								</p>
								<Button
									variant="subtle"
									label="Run health check"
									iconLeft="activity"
									:loading="linting"
									@click="confirmLint"
								/>
							</div>
							<p class="mt-2 text-p-sm text-ink-gray-5">
								<template v-if="caps.wiki_lint_last_run_at">
									Last health check {{ timeAgo(caps.wiki_lint_last_run_at)
									}}<span v-if="caps.wiki_lint_summary">
										- {{ caps.wiki_lint_summary }}</span
									>
								</template>
								<template v-else>Health check hasn't run yet.</template>
							</p>
						</div>
					</template>
				</Popover>
				<Button
					v-if="caps.creatable_scopes.length"
					variant="solid"
					label="New page"
					iconLeft="plus"
					@click="openCreate"
				/>
			</template>

			<!-- persistent orientation: the teaching copy otherwise lives only in
			     the empty state, which real first-visits on a grown wiki never see -->
			<template #banner>
				<p class="mb-2 text-p-sm text-ink-gray-5">
					The wiki is the knowledge {{ agentName }} keeps about your business -
					customers, suppliers, processes, conventions. It grows from chat and voice
					notes; {{ agentName }}
					cites it when answering.
				</p>
			</template>

			<template #cell-title="{ row }">
				<div class="flex items-center gap-2 overflow-hidden">
					<div class="truncate text-base">{{ row.title || row.slug }}</div>
					<Badge
						v-if="row.contradiction_flag"
						variant="subtle"
						theme="red"
						label="Conflicting"
					/>
					<Badge v-if="row.stale" variant="subtle" theme="orange" label="Stale" />
				</div>
			</template>

			<template #cell-page_type="{ row }">
				<Badge variant="outline" theme="gray" :label="typeLabel(row.page_type)" />
			</template>

			<template #cell-scope="{ row }">
				<Tooltip :text="scopeTooltip(row)">
					<Badge
						variant="subtle"
						:theme="SCOPE_THEME[row.scope] || 'gray'"
						:label="row.scope || 'Org'"
					/>
				</Tooltip>
			</template>

			<template #cell-summary="{ row }">
				<div class="truncate text-base text-ink-gray-6">{{ row.summary }}</div>
			</template>

			<template #cell-modified="{ row }">
				<div class="flex w-full items-center justify-end">
					<Tooltip :text="exactDate(row.modified)">
						<div class="truncate text-base">{{ timeAgo(row.modified) }}</div>
					</Tooltip>
				</div>
			</template>

			<!-- row actions (server-computed can_archive; delete shares the archive
			     authority server-side). @click.stop.prevent so the buttons never
			     bubble into the row's open-dialog click (RunsTab idiom). -->
			<template #cell-_actions="{ row }">
				<div
					v-if="row.can_archive"
					class="flex w-full items-center justify-end gap-1"
					@click.stop.prevent
				>
					<Button
						v-if="!isArchivedView"
						variant="ghost"
						icon="archive"
						:tooltip="'Archive'"
						:loading="rowBusy === rowSlug(row)"
						@click="confirmArchive(row)"
					/>
					<Button
						v-else
						variant="ghost"
						icon="rotate-ccw"
						:tooltip="'Restore'"
						:loading="rowBusy === rowSlug(row)"
						@click="doRestore(row)"
					/>
					<Button
						variant="ghost"
						theme="red"
						icon="trash-2"
						:tooltip="'Delete permanently'"
						:loading="rowBusy === rowSlug(row)"
						@click="confirmDelete(row)"
					/>
				</div>
			</template>
		</ListPage>

		<WikiPageDialog v-model="pageDialog.show" :slug="pageDialog.slug" @refresh="refreshKeep" />

		<!-- New page dialog: scope options limited to what the caller may create;
		     the server derives (and suffixes) the slug from type + title -->
		<Dialog v-model="createDialog.show" :options="{ title: 'New wiki page', size: 'md' }">
			<template #body-content>
				<div class="flex flex-col gap-3">
					<FormControl
						type="text"
						label="Title"
						placeholder="e.g. Acme Industries payment terms"
						:modelValue="createDialog.title"
						@update:modelValue="(v) => (createDialog.title = v)"
					/>
					<div class="flex flex-col gap-1">
						<FormControl
							type="select"
							label="Type"
							:options="TYPE_SELECT_OPTIONS"
							:modelValue="createDialog.page_type"
							@update:modelValue="(v) => (createDialog.page_type = v)"
						/>
						<p
							v-if="TYPE_HELP[createDialog.page_type]"
							class="text-p-sm text-ink-gray-5"
						>
							{{ TYPE_HELP[createDialog.page_type] }}
						</p>
					</div>
					<FormControl
						type="select"
						label="Scope"
						:options="scopeSelectOptions"
						:modelValue="createDialog.scope"
						@update:modelValue="(v) => (createDialog.scope = v)"
					/>
					<div v-if="createDialog.scope === 'Role'" class="flex flex-col gap-1">
						<span class="block text-xs text-ink-gray-5">Role</span>
						<!-- Autocomplete: SMs see every targetable role (~dozens);
						     a plain select is unusable at that size -->
						<Autocomplete
							placeholder="Search roles"
							:options="roleSelectOptions"
							:modelValue="createDialog.target_role"
							@update:modelValue="
								(v) => (createDialog.target_role = (v && v.value) || '')
							"
						/>
						<p v-if="!caps.is_sm" class="text-p-sm text-ink-gray-5">
							You can share knowledge with roles you hold yourself; an administrator
							can target any role.
						</p>
					</div>
					<FormControl
						type="textarea"
						label="Summary (optional)"
						:rows="2"
						:placeholder="`One or two lines ${agentName} can cite in chat context`"
						:modelValue="createDialog.summary"
						@update:modelValue="(v) => (createDialog.summary = v)"
					/>
					<FormControl
						type="textarea"
						label="Content (markdown, optional)"
						:rows="5"
						placeholder="What should this page say? You can also add content later."
						:modelValue="createDialog.body_md"
						@update:modelValue="(v) => (createDialog.body_md = v)"
					/>
					<p v-if="slugPreview" class="text-p-sm text-ink-gray-5">
						Page id: {{ slugPreview }}
					</p>
				</div>
			</template>
			<template #actions>
				<div class="flex flex-col gap-1.5">
					<div class="flex items-center gap-2">
						<Button
							variant="solid"
							label="Create"
							:loading="createDialog.saving"
							:disabled="!canCreate"
							@click="doCreate"
						/>
						<Button label="Cancel" @click="createDialog.show = false" />
					</div>
					<p v-if="!canCreate && createMissing" class="text-p-sm text-ink-gray-5">
						{{ createMissing }}
					</p>
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
// WikiTab - the "Wiki" tab inside the Skills page (design D4): the org-wide
// knowledge base Jarvis maintains, now scope-aware (Org / Role / User pages,
// server-side visibility). Standard ListPage + useListPage kit (FilesList
// precedent): server pagination, debounced search, scope / type / attention
// quick filters. Rows open WikiPageDialog (view/edit/archive gated by the
// server's can_edit/can_archive flags); a trailing actions column offers
// Archive (Restore on the Archived view) and permanent Delete inline for
// rows the caller may manage. "New page" shows for anyone whose
// creatable_scopes isn't empty; SMs also get the settings popover (knowledge
// language, mirror sync, health check).
import { reactive, ref, computed, onMounted } from "vue";
import {
	Autocomplete,
	Badge,
	Button,
	Dialog,
	FormControl,
	Popover,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import { sessionUser } from "@/data/session";
import ListPage from "@/components/list/ListPage.vue";
import WikiPageDialog from "@/components/wiki/WikiPageDialog.vue";
import { useListPage } from "@/composables/useListPage";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	listWikiPagesPage,
	getWikiCaps,
	createWikiPage,
	archiveWikiPage,
	restoreWikiPage,
	deleteWikiPage,
	setKnowledgeLanguage,
	syncWikiMirrorNow,
	runWikiLintNow,
} from "@/api/wiki";
import { agentName } from "@/branding";

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── static config ────────────────────────────────────────────────────────────
const WIKI_TYPES = [
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
const TYPE_OPTIONS = [
	{ label: "All types", value: "" },
	...WIKI_TYPES.map((t) => ({ label: t === "Org" ? "Org notes" : t, value: t })),
];
const TYPE_SELECT_OPTIONS = [
	{ label: "Select a type", value: "" },
	...WIKI_TYPES.map((t) => ({ label: t === "Org" ? "Org notes" : t, value: t })),
];
const SCOPE_THEME = { Org: "gray", Role: "blue", User: "green" };
const SCOPE_OPTIONS = [
	{ label: "All scopes", value: "" },
	{ label: "Org", value: "org" },
	{ label: "My role", value: "role" },
	{ label: "Mine", value: "mine" },
];
const ATTENTION_OPTIONS = [
	{ label: "All pages", value: "" },
	{ label: "Needs attention", value: "1" },
	{ label: "Archived", value: "archived" },
];
// "Org" as a page TYPE collides visually with the "Org" scope badge one
// column over; display it under a clearer name (stored value unchanged).
const TYPE_LABELS = { Org: "Org notes" };
function typeLabel(t) {
	return TYPE_LABELS[t] || t;
}
// one-line explanations for the create dialog's Type select
const TYPE_HELP = {
	Customer: "One specific customer's quirks - payment habits, contacts, gotchas.",
	Supplier: "One specific supplier's quirks - lead times, terms, who to call.",
	Item: "One item or item group - variants, storage, known issues.",
	Process: "A procedure as your org actually runs it - steps, owners, exceptions.",
	Doctype: "Org-wide conventions on a document type, e.g. Sales Invoice habits.",
	Exception: "A known edge case or standing workaround.",
	Integration: "An external system your org connects to and its rules.",
	People: "Who does what - approvers, escalation paths, contacts.",
	Org: "General org-level facts that fit nowhere else.",
};
const LANGUAGE_OPTIONS = [
	{ label: "English (recommended)", value: "English" },
	{ label: "Original language", value: "Original" },
];
const SCOPE_LABELS = {
	Org: "Org - visible to everyone",
	Role: "Role - people holding a role",
	User: "Personal - just me",
};

const columns = [
	{ label: "Title", key: "title", width: 3 },
	{ label: "Type", key: "page_type", width: "8rem" },
	{ label: "Scope", key: "scope", width: "6rem" },
	{ label: "Summary", key: "summary", width: 4 },
	{ label: "Updated", key: "modified", width: "8rem", align: "right" },
	{ label: "", key: "_actions", width: "7rem", align: "right" },
];
// search rides the quick-filter strip (FilesList precedent): it lives in the
// filters object so the input stays controlled, and fetchFn moves it onto the
// endpoint's `search` kwarg.
const quickFilters = [
	{ key: "search", label: "Search pages", type: "text" },
	{ key: "scope", label: "Scope", type: "select", options: SCOPE_OPTIONS },
	{ key: "page_type", label: "Type", type: "select", options: TYPE_OPTIONS },
	// "View", not "Attention": it also carries the Archived lifecycle view
	{ key: "attention", label: "View", type: "select", options: ATTENTION_OPTIONS },
];
const filterDefs = [
	{ key: "scope", label: "Scope", type: "select", options: SCOPE_OPTIONS },
	{ key: "page_type", label: "Type", type: "select", options: TYPE_OPTIONS },
	{ key: "attention", label: "View", type: "select", options: ATTENTION_OPTIONS },
];

// ── list state (server envelope; endpoint paginates by page number) ──────────
const {
	rows,
	total,
	hasMore,
	loading,
	error,
	filters,
	setFilters,
	pageLength,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: (p) => {
		const f = p.filters || {};
		const pl = p.page_length || 20;
		return listWikiPagesPage({
			search: f.search || p.search || "",
			page_type: f.page_type || "",
			scope_filter: f.scope || "all",
			attention: f.attention === "1" ? 1 : 0,
			archived: f.attention === "archived" ? 1 : 0,
			// kit sends a start offset; the endpoint takes a page number
			page: Math.floor((p.start || 0) / pl) + 1,
			page_length: pl,
		});
	},
	storageKey: "wiki",
});

// Archive vs Restore in the actions column keys off the lifecycle view:
// the Archived view lists only Archived pages, every other view only Active.
const isArchivedView = computed(() => filters.attention === "archived");

const emptyState = computed(() => {
	if (filters.scope === "mine" && caps.creatable_scopes.includes("User"))
		return {
			icon: "book-open",
			title: "No personal pages yet",
			description:
				`Personal pages are knowledge only you and ${agentName} share - shortcuts, ` +
				'preferences, your own working notes. Use "New page" and pick the ' +
				"Personal scope to create your first one.",
		};
	if (Object.keys(filters).length)
		return {
			icon: "book-open",
			title: "No pages match",
			description: "Try a different search, scope or type filter.",
		};
	return {
		icon: "book-open",
		title: "No wiki pages yet",
		description:
			`The wiki is the knowledge base ${agentName} keeps about your business - customers, ` +
			"suppliers, items and processes. It grows on its own as people answer chat " +
			"nudges and record voice notes on the Business tab; pages appear here as " +
			`${agentName} learns.`,
	};
});

function scopeTooltip(row) {
	if (row.scope === "Role") return `Visible to people with role: ${row.target_role || "-"}`;
	if (row.scope === "User")
		return `Personal page - visible only to ${row.target_user || "its owner"}`;
	return "Visible to everyone";
}

// ── caps (creatable scopes + SM header extras) ───────────────────────────────
const caps = reactive({
	is_sm: false,
	creatable_scopes: [],
	manageable_roles: [],
	knowledge_language: "English",
	wiki_lint_last_run_at: null,
	wiki_lint_summary: "",
	wiki_mirror_last_synced_at: null,
	wiki_mirror_last_sync_status: "",
});

async function loadCaps() {
	try {
		const c = await getWikiCaps();
		caps.is_sm = !!c.is_sm;
		caps.creatable_scopes = c.creatable_scopes || [];
		caps.manageable_roles = c.manageable_roles || [];
		caps.knowledge_language = c.knowledge_language || "English";
		caps.wiki_lint_last_run_at = c.wiki_lint_last_run_at || null;
		caps.wiki_lint_summary = c.wiki_lint_summary || "";
		caps.wiki_mirror_last_synced_at = c.wiki_mirror_last_synced_at || null;
		caps.wiki_mirror_last_sync_status = c.wiki_mirror_last_sync_status || "";
	} catch (e) {
		// read-only view stays useful without caps (no create / SM chrome)
	}
}

// ── page viewer dialog ───────────────────────────────────────────────────────
const pageDialog = reactive({ show: false, slug: "" });
function openRow(row) {
	openPage(row.slug || row.name);
}
function openPage(slug) {
	pageDialog.slug = slug;
	pageDialog.show = true;
}

// ── row actions (archive / restore / delete) ─────────────────────────────────
// slug of the row with an in-flight lifecycle call - one at a time is plenty
const rowBusy = ref("");
function rowSlug(row) {
	return row.slug || row.name;
}

async function runRowAction(row, fn, successMsg) {
	const slug = rowSlug(row);
	rowBusy.value = slug;
	try {
		await fn(slug);
		toast.success(successMsg);
		resetLoad();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		rowBusy.value = "";
	}
}

function confirmArchive(row) {
	confirmDialog({
		title: "Archive this page?",
		message:
			"Archived pages stop appearing in the list and are no longer used as chat context. The record is kept.",
		onConfirm: async ({ hideDialog }) => {
			hideDialog();
			await runRowAction(row, archiveWikiPage, "Page archived");
		},
	});
}

// no confirm: Restore is itself the escape hatch for an accidental archive
function doRestore(row) {
	runRowAction(row, restoreWikiPage, "Page restored");
}

function confirmDelete(row) {
	confirmDialog({
		title: "Delete this page permanently?",
		message:
			"Permanently deletes this page - archiving keeps it recoverable. This cannot be undone.",
		onConfirm: async ({ hideDialog }) => {
			hideDialog();
			await runRowAction(row, deleteWikiPage, "Page deleted");
		},
	});
}

// ── create dialog ────────────────────────────────────────────────────────────
const createDialog = reactive({
	show: false,
	title: "",
	page_type: "",
	scope: "Org",
	target_role: "",
	summary: "",
	body_md: "",
	saving: false,
});

const scopeSelectOptions = computed(() =>
	(caps.creatable_scopes || []).map((s) => ({ label: SCOPE_LABELS[s] || s, value: s }))
);
const roleSelectOptions = computed(() =>
	(caps.manageable_roles || []).map((r) => ({ label: r, value: r }))
);
// Preview of the server-derived slug (`<type>--<scrubbed-title>` plus the
// controller's audience suffix for non-Org scopes) - mirror it fully so the
// preview never lies about the final page id.
const scrub = (s) =>
	String(s || "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "");
const slugPreview = computed(() => {
	const base = scrub(createDialog.title);
	if (!base || !createDialog.page_type) return "";
	let slug = `${createDialog.page_type.toLowerCase()}--${base}`;
	if (createDialog.scope === "User")
		slug += `--u-${scrub(String(sessionUser() || "").split("@")[0]) || "me"}`;
	else if (createDialog.scope === "Role" && createDialog.target_role)
		slug += `--r-${scrub(createDialog.target_role)}`;
	return slug;
});
const canCreate = computed(
	() =>
		!!createDialog.title.trim() &&
		!!createDialog.page_type &&
		!!createDialog.scope &&
		(createDialog.scope !== "Role" || !!createDialog.target_role)
);
const createMissing = computed(() => {
	const missing = [];
	if (!createDialog.title.trim()) missing.push("a title");
	if (!createDialog.page_type) missing.push("a type");
	if (createDialog.scope === "Role" && !createDialog.target_role) missing.push("a role");
	return missing.length ? `Still needed: ${missing.join(", ")}.` : "";
});

function openCreate() {
	createDialog.title = "";
	createDialog.page_type = "";
	createDialog.scope = caps.creatable_scopes[0] || "Org";
	createDialog.target_role = "";
	createDialog.summary = "";
	createDialog.body_md = "";
	createDialog.show = true;
}

async function doCreate() {
	createDialog.saving = true;
	try {
		const res = await createWikiPage({
			title: createDialog.title.trim(),
			page_type: createDialog.page_type,
			scope: createDialog.scope,
			target_role: createDialog.scope === "Role" ? createDialog.target_role : "",
			summary: createDialog.summary,
			body_md: createDialog.body_md,
		});
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not create the page.");
		} else {
			createDialog.show = false;
			toast.success("Page created");
			resetLoad();
			openPage(res.slug);
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		createDialog.saving = false;
	}
}

// ── SM extras (settings popover) ─────────────────────────────────────────────
const syncing = ref(false);
const linting = ref(false);

async function changeLanguage(v) {
	if (!v || v === caps.knowledge_language) return;
	const previous = caps.knowledge_language;
	try {
		await setKnowledgeLanguage(v);
		caps.knowledge_language = v;
		toast.success(`Knowledge language set to ${v}`);
	} catch (e) {
		caps.knowledge_language = previous;
		toast.error(errMsg(e));
	}
}

function confirmSync() {
	confirmDialog({
		title: "Sync wiki to agent now?",
		message:
			"Pushes every active org-scope page into the agent's workspace mirror now, " +
			"instead of waiting for the next change or the daily sync.",
		onConfirm: async ({ hideDialog }) => {
			syncing.value = true;
			try {
				const r = await syncWikiMirrorNow();
				hideDialog();
				if (r && r.ok === false) toast.error(r.reason || "Could not queue the sync.");
				else toast.success("Sync queued");
			} catch (e) {
				toast.error(errMsg(e));
			} finally {
				syncing.value = false;
			}
		},
	});
}

function confirmLint() {
	confirmDialog({
		title: "Run wiki health check?",
		message:
			"Scans active pages for contradictions, stale content, orphans and " +
			"near-duplicate titles, and flags pages needing attention. May take a moment.",
		onConfirm: async ({ hideDialog }) => {
			linting.value = true;
			try {
				const r = await runWikiLintNow();
				hideDialog();
				if (r && r.ok === false)
					toast.error(r.reason || "Could not run the health check.");
				else toast.success("Health check finished");
				loadCaps();
				refreshKeep();
			} catch (e) {
				toast.error(errMsg(e));
			} finally {
				linting.value = false;
			}
		},
	});
}

// ── init ─────────────────────────────────────────────────────────────────────
onMounted(loadCaps);
</script>
