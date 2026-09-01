<template>
	<DocPage
		:breadcrumbs="breadcrumbs"
		:title="pageTitle"
		:status-badge="null"
		:dirty="dirty"
		:loading="loading"
		:error="loadError"
	>
		<template #actions>
			<PromotionStatusChip v-if="!isNew && page && myPromo" :req="myPromo" noun="page" />
			<Badge
				v-if="!isNew && page && !page.can_edit"
				variant="subtle"
				theme="gray"
				size="lg"
				label="Read-only"
			/>
			<Dropdown v-if="!isNew && page && overflowOptions.length" :options="overflowOptions">
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
			<Button
				v-if="!isNew && page && page.can_edit && !editing"
				variant="subtle"
				label="Edit"
				iconLeft="edit-2"
				@click="startEdit"
			/>
			<Button v-if="editing" label="Cancel" :disabled="saving" @click="cancelEdit" />
			<Button
				v-if="isNew || editing"
				variant="solid"
				:label="isNew ? 'Create' : 'Save'"
				:disabled="isNew && !canCreate"
				:loading="saving"
				@click="isNew ? doCreate() : save()"
			/>
		</template>

		<template #main>
			<!-- ── new page: the create form (fields ported from WikiTab's former
			     dialog; scope is freely selectable only here, at creation) ── -->
			<DocSection v-if="isNew" label="Details">
				<div class="space-y-4">
					<FormControl
						type="text"
						label="Title"
						required
						placeholder="e.g. Acme Industries payment terms"
						:modelValue="form.title"
						:disabled="saving"
						@update:modelValue="(v) => (form.title = v)"
					/>
					<div class="flex flex-col gap-1">
						<FormControl
							type="select"
							label="Type"
							:options="TYPE_SELECT_OPTIONS"
							:modelValue="form.page_type"
							:disabled="saving"
							@update:modelValue="(v) => (form.page_type = v)"
						/>
						<p v-if="TYPE_HELP[form.page_type]" class="text-p-sm text-ink-gray-5">
							{{ TYPE_HELP[form.page_type] }}
						</p>
					</div>
					<FormControl
						type="select"
						label="Scope"
						:options="scopeSelectOptions"
						:modelValue="form.scope"
						:disabled="saving"
						@update:modelValue="(v) => (form.scope = v)"
					/>
					<div v-if="form.scope === 'Role'" class="flex flex-col gap-1">
						<span class="block text-xs text-ink-gray-5">Role</span>
						<Autocomplete
							placeholder="Search roles"
							:options="roleSelectOptions"
							:modelValue="form.target_role"
							@update:modelValue="(v) => (form.target_role = (v && v.value) || '')"
						/>
						<p v-if="!caps.is_sm" class="text-p-sm text-ink-gray-5">
							You can share knowledge with roles you hold yourself. An administrator
							can target any role.
						</p>
					</div>
					<FormControl
						type="textarea"
						label="Summary (optional)"
						:rows="2"
						:placeholder="`One or two lines ${agentName} can cite in chat context`"
						:modelValue="form.summary"
						:disabled="saving"
						@update:modelValue="(v) => (form.summary = v)"
					/>
					<FormControl
						type="textarea"
						label="Content (markdown, optional)"
						:rows="10"
						placeholder="What should this page say? You can also add content later."
						:modelValue="form.body_md"
						:disabled="saving"
						@update:modelValue="(v) => (form.body_md = v)"
					/>
					<p v-if="slugPreview" class="text-p-sm text-ink-gray-5">
						Page id: {{ slugPreview }}
					</p>
					<p v-if="!canCreate && createMissing" class="text-p-sm text-ink-gray-5">
						{{ createMissing }}
					</p>
				</div>
			</DocSection>

			<!-- ── existing page: view (rendered markdown) or edit ── -->
			<template v-else-if="page">
				<!-- metadata row: type · scope (+target) · slug · updated · flags.
				     View mode shows these as a labeled Details section below (reads
				     like the create form); this compact strip stays as edit-mode
				     context only. -->
				<div v-if="editing" class="flex flex-wrap items-center gap-2 text-sm">
					<Badge
						variant="outline"
						theme="gray"
						:label="page.page_type === 'Org' ? 'Org notes' : page.page_type"
					/>
					<Badge
						variant="subtle"
						:theme="SCOPE_THEME[page.scope] || 'gray'"
						:label="page.scope || 'Org'"
					/>
					<span v-if="scopeTarget" class="text-ink-gray-5">for {{ scopeTarget }}</span>
					<Badge
						v-if="page.status === 'Archived'"
						variant="subtle"
						theme="gray"
						label="Archived"
					/>
					<span class="text-ink-gray-5">{{ page.slug }}</span>
					<Tooltip v-if="updatedAt" :text="exactDate(updatedAt)">
						<span class="text-ink-gray-5">updated {{ timeAgo(updatedAt) }}</span>
					</Tooltip>
					<Badge
						v-if="page.contradiction_flag"
						variant="subtle"
						theme="red"
						label="Conflicting"
					/>
					<Badge v-if="page.stale" variant="subtle" theme="orange" label="Stale" />
				</div>

				<div
					v-if="page.contradiction_flag"
					class="mt-3 rounded-lg border border-outline-red-2 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
				>
					People have reported conflicting facts. Look for the "Contradiction flagged"
					section below.{{
						page.can_edit
							? " Edit the page to keep the correct version. Saving marks the conflict resolved."
							: " A wiki manager can edit the page to resolve it."
					}}
				</div>
				<div
					v-if="page.stale"
					class="mt-3 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
				>
					Not confirmed in 90+ days{{
						page.can_edit ? ". Saving an edit marks it reviewed." : "."
					}}
				</div>

				<template v-if="editing">
					<DocSection label="Edit page">
						<div class="space-y-4">
							<FormControl
								type="text"
								label="Title"
								:modelValue="editTitle"
								:disabled="saving"
								@update:modelValue="(v) => (editTitle = v)"
							/>
							<FormControl
								type="textarea"
								label="Summary"
								:rows="2"
								:modelValue="editSummary"
								:disabled="saving"
								@update:modelValue="(v) => (editSummary = v)"
							/>
							<!-- lg+: textarea and live preview side by side; below lg the
							     Preview button toggles between them (WikiPageDialog idiom). -->
							<div class="grid gap-4 lg:grid-cols-2">
								<FormControl
									type="textarea"
									:class="previewing ? 'hidden lg:block' : ''"
									label="Body (markdown)"
									:rows="14"
									:modelValue="editBody"
									:disabled="saving"
									@update:modelValue="(v) => (editBody = v)"
								/>
								<div :class="previewing ? '' : 'hidden lg:block'">
									<div class="text-xs text-ink-gray-5">Preview</div>
									<div
										class="prose prose-sm mt-1 max-h-[24rem] max-w-none overflow-y-auto rounded border px-3 py-2"
										v-html="previewHtml"
									/>
								</div>
							</div>
							<Button
								class="lg:hidden"
								variant="ghost"
								:label="previewing ? 'Back to editing' : 'Preview'"
								:iconLeft="previewing ? 'edit-2' : 'eye'"
								@click="previewing = !previewing"
							/>
							<!-- scope governance: type/scope are read-only here by design -
							     changing scope goes through the reviewer queue, never a
							     free dropdown on an existing page (save_wiki_page accepts
							     only title/summary/body_md). -->
							<p class="text-p-sm text-ink-gray-5">
								Type and scope can't be changed here.<template
									v-if="canPromote && !promoPending"
								>
									Use "Request promotion" to widen who can see this
									page.</template
								>
							</p>
						</div>
					</DocSection>
				</template>
				<template v-else>
					<!-- Labeled read view: same field shape as the create form so a
					     saved page reads as a filled-in form, not a bare blob. -->
					<DocSection label="Details">
						<dl class="grid gap-x-6 gap-y-4 sm:grid-cols-2">
							<div>
								<dt class="text-xs text-ink-gray-5">Type</dt>
								<dd class="mt-1 text-base text-ink-gray-8">
									{{ page.page_type === "Org" ? "Org notes" : page.page_type }}
								</dd>
							</div>
							<div>
								<dt class="text-xs text-ink-gray-5">Scope</dt>
								<dd class="mt-1 flex flex-wrap items-center gap-2">
									<Badge
										variant="subtle"
										:theme="SCOPE_THEME[page.scope] || 'gray'"
										:label="page.scope || 'Org'"
									/>
									<span v-if="scopeTarget" class="text-sm text-ink-gray-6"
										>for {{ scopeTarget }}</span
									>
								</dd>
							</div>
							<div>
								<dt class="text-xs text-ink-gray-5">Page id</dt>
								<dd class="mt-1 break-all text-base text-ink-gray-8">
									{{ page.slug }}
								</dd>
							</div>
							<div>
								<dt class="text-xs text-ink-gray-5">Updated</dt>
								<dd class="mt-1 text-base text-ink-gray-8">
									<Tooltip v-if="updatedAt" :text="exactDate(updatedAt)">
										<span>{{ timeAgo(updatedAt) }}</span>
									</Tooltip>
									<span v-else>Not recorded</span>
								</dd>
							</div>
							<div
								v-if="
									page.status === 'Archived' ||
									page.contradiction_flag ||
									page.stale
								"
								class="sm:col-span-2"
							>
								<dt class="text-xs text-ink-gray-5">Status</dt>
								<dd class="mt-1 flex flex-wrap gap-2">
									<Badge
										v-if="page.status === 'Archived'"
										variant="subtle"
										theme="gray"
										label="Archived"
									/>
									<Badge
										v-if="page.contradiction_flag"
										variant="subtle"
										theme="red"
										label="Conflicting"
									/>
									<Badge
										v-if="page.stale"
										variant="subtle"
										theme="orange"
										label="Stale"
									/>
								</dd>
							</div>
							<div class="sm:col-span-2">
								<dt class="text-xs text-ink-gray-5">Summary</dt>
								<dd v-if="page.summary" class="mt-1 text-sm text-ink-gray-8">
									{{ page.summary }}
								</dd>
								<dd v-else class="mt-1 text-sm text-ink-gray-5">
									No summary yet.
								</dd>
							</div>
							<div class="sm:col-span-2">
								<dt class="text-xs text-ink-gray-5">Content</dt>
								<!-- renderMarkdown from @/markdown (escapes HTML first - safe) -->
								<dd
									v-if="page.body_md"
									class="prose prose-sm mt-1 max-w-none"
									v-html="bodyHtml"
								/>
								<dd v-else class="mt-1 text-sm text-ink-gray-5">
									No content yet.
								</dd>
							</div>
						</dl>
						<!-- provenance: where Jarvis learned this - earns trust and edits -->
						<p v-if="provenance" class="mt-4 border-t pt-2 text-p-sm text-ink-gray-5">
							{{ provenance }}
						</p>
					</DocSection>
				</template>
			</template>
		</template>
	</DocPage>

	<PromotionRequestDialog
		v-model="promoDialog"
		noun="page"
		:busy="promoBusy"
		@submit="submitPromotion"
	/>
</template>

<script setup>
// WikiDetail - the routed /skills/wiki/new + /skills/wiki/:slug page
// (replaces the old dialogs: WikiTab's "New page" Dialog and
// components/wiki/WikiPageDialog.vue), built on the same DocPage frame
// SkillDetail uses. isNew renders the create form (Title/Type/Scope/Summary/
// Content - scope freely selectable only here); an existing slug renders the
// rendered-markdown view with an Edit toggle, mirroring WikiPageDialog's
// view/edit split exactly, just as a page instead of a popup.
//
// SCOPE GOVERNANCE: save_wiki_page (jarvis/chat/wiki.py) accepts only
// {title, summary, body_md} - no scope/target_role/target_user - so edit mode
// never offers a scope dropdown. Widening scope on an existing page goes
// through PromotionRequestDialog into the reviewer queue, same as the dialog
// this page replaces.
import { reactive, ref, computed, watch } from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Autocomplete,
	Badge,
	Button,
	Dropdown,
	FormControl,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import DocPage from "@/components/doc/DocPage.vue";
import DocSection from "@/components/doc/DocSection.vue";
import PromotionRequestDialog from "@/components/skills/PromotionRequestDialog.vue";
import PromotionStatusChip from "@/components/skills/PromotionStatusChip.vue";
import { sessionUser } from "@/data/session";
import { renderMarkdown } from "@/markdown";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	getWikiCaps,
	getWikiPage,
	createWikiPage,
	saveWikiPage,
	archiveWikiPage,
	restoreWikiPage,
	deleteWikiPage,
	requestWikiPromotion,
	myWikiPromotion,
} from "@/api/wiki";
import { agentName } from "@/branding";
import { errMessage as errMsg, errHtml } from "@/lib/errors";
import { WIKI_TYPES, SCOPE_THEME, scrub } from "@/lib/wikiMeta";

const props = defineProps({
	slug: { type: String, default: "" },
	isNew: { type: Boolean, default: false },
});

const router = useRouter();

const TYPE_SELECT_OPTIONS = [
	{ label: "Select a type", value: "" },
	...WIKI_TYPES.map((t) => ({ label: t === "Org" ? "Org notes" : t, value: t })),
];
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
const SCOPE_LABELS = {
	Org: "Org - visible to everyone",
	Role: "Role - people holding a role",
	User: "Personal - just me",
};
// ── caps (creatable scopes + roles, for the create form only) ───────────────
const caps = reactive({ creatable_scopes: [], manageable_roles: [], is_sm: false });
// Returns false on a failed call so init() can show a real error instead of
// silently rendering a Scope select with zero options: WikiTab only shows
// "New page" once caps.creatable_scopes is known non-empty, but this route is
// deep-linkable, so a caller here can arrive with no caps loaded at all.
async function loadCaps() {
	try {
		const c = await getWikiCaps();
		caps.creatable_scopes = c.creatable_scopes || [];
		caps.manageable_roles = c.manageable_roles || [];
		caps.is_sm = !!c.is_sm;
		return true;
	} catch (e) {
		return false;
	}
}

// ── create form (new page) ───────────────────────────────────────────────────
const form = reactive({
	title: "",
	page_type: "",
	scope: "Org",
	target_role: "",
	summary: "",
	body_md: "",
});
function resetCreateForm() {
	form.title = "";
	form.page_type = "";
	form.scope = caps.creatable_scopes[0] || "Org";
	form.target_role = "";
	form.summary = "";
	form.body_md = "";
}
const scopeSelectOptions = computed(() =>
	(caps.creatable_scopes || []).map((s) => ({ label: SCOPE_LABELS[s] || s, value: s }))
);
const roleSelectOptions = computed(() =>
	(caps.manageable_roles || []).map((r) => ({ label: r, value: r }))
);
// Preview of the server-derived slug - mirrors WikiTab's former preview so it
// never lies about the final page id.
const slugPreview = computed(() => {
	const base = scrub(form.title);
	if (!base || !form.page_type) return "";
	let slug = `${form.page_type.toLowerCase()}--${base}`;
	if (form.scope === "User")
		slug += `--u-${scrub(String(sessionUser() || "").split("@")[0]) || "me"}`;
	else if (form.scope === "Role" && form.target_role) slug += `--r-${scrub(form.target_role)}`;
	return slug;
});
const canCreate = computed(
	() =>
		!!form.title.trim() &&
		!!form.page_type &&
		!!form.scope &&
		(form.scope !== "Role" || !!form.target_role)
);
const createMissing = computed(() => {
	const missing = [];
	if (!form.title.trim()) missing.push("a title");
	if (!form.page_type) missing.push("a type");
	if (form.scope === "Role" && !form.target_role) missing.push("a role");
	return missing.length ? `Still needed: ${missing.join(", ")}.` : "";
});

async function doCreate() {
	if (!canCreate.value) return;
	saving.value = true;
	try {
		const res = await createWikiPage({
			title: form.title.trim(),
			page_type: form.page_type,
			scope: form.scope,
			target_role: form.scope === "Role" ? form.target_role : "",
			summary: form.summary,
			body_md: form.body_md,
		});
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not create the page.");
		} else {
			toast.success("Page created");
			bypassGuard = true;
			router.replace({ name: "WikiPageDetail", params: { slug: res.slug } });
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

// ── existing page: load / view / edit ────────────────────────────────────────
const page = ref(null);
const loading = ref(false);
const loadError = ref("");
const saving = ref(false);
const editing = ref(false);
const previewing = ref(false);
const editTitle = ref("");
const editSummary = ref("");
const editBody = ref("");
const archiving = ref(false);
const deleting = ref(false);

const pageTitle = computed(() =>
	props.isNew ? form.title || "New wiki page" : (page.value && page.value.title) || props.slug
);
const breadcrumbs = computed(() => [
	{ label: "Skills", route: { name: "SkillsList" } },
	{ label: "Wiki", route: { name: "SkillsList", hash: "#wiki" } },
	props.isNew
		? { label: "New page", route: { name: "WikiPageNew" } }
		: {
				label: pageTitle.value,
				route: { name: "WikiPageDetail", params: { slug: props.slug } },
		  },
]);

const bodyHtml = computed(() =>
	page.value && page.value.body_md ? renderMarkdown(page.value.body_md) : ""
);
const previewHtml = computed(() =>
	editBody.value
		? renderMarkdown(editBody.value)
		: '<p class="text-ink-gray-5">Nothing to preview yet.</p>'
);
const updatedAt = computed(
	() => (page.value && (page.value.modified || page.value.last_confirmed_at)) || ""
);
const scopeTarget = computed(() => {
	if (!page.value) return "";
	if (page.value.scope === "Role") return page.value.target_role || "";
	if (page.value.scope === "User") return page.value.target_user || "";
	return "";
});
// "From a voice note by X, Jul 7" - the page's latest source entry.
const provenance = computed(() => {
	const sources = (page.value && page.value.sources) || [];
	if (!Array.isArray(sources) || !sources.length) return "";
	const s = sources[sources.length - 1] || {};
	const kindLabel =
		{ voice: "a voice note", chat: "a chat conversation", edit: "a manual edit" }[s.kind] ||
		(s.kind ? String(s.kind) : "a recorded source");
	const who = s.user ? ` by ${s.user}` : "";
	const when = s.date ? `, ${s.date}` : "";
	const count = sources.length > 1 ? ` · ${sources.length} sources in total` : "";
	return `Latest source: ${kindLabel}${who}${when}${count}`;
});

// ── promotion (requester side) ───────────────────────────────────────────────
const myPromo = ref(null);
const promoDialog = ref(false);
const promoBusy = ref(false);
const canPromote = computed(
	() =>
		!!page.value &&
		page.value.scope === "User" &&
		!!page.value.can_edit &&
		page.value.status !== "Archived"
);
const promoPending = computed(() => !!(myPromo.value && myPromo.value.status === "Pending"));

async function loadMyPromo() {
	myPromo.value = null;
	if (!page.value || page.value.scope !== "User" || !page.value.can_edit) return;
	// Captured before the await; the loadConversation idiom (ChatView.vue) -
	// drop the result if the route moved to a different page while this
	// request was in flight, so a slow response can't clobber the chip for
	// the page the user actually navigated to.
	const slug = props.slug;
	try {
		const res = await myWikiPromotion(slug);
		if (props.slug !== slug) return;
		myPromo.value = res && res.status ? res : null;
	} catch {
		// best-effort chip
	}
}

async function submitPromotion({ to_scope, target_role, note }) {
	promoBusy.value = true;
	try {
		await requestWikiPromotion({ page: props.slug, to_scope, target_role, note });
		promoDialog.value = false;
		toast.success("Promotion requested. A reviewer will decide.");
		await loadMyPromo();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		promoBusy.value = false;
	}
}

function startEdit() {
	previewing.value = false;
	editTitle.value = (page.value && page.value.title) || "";
	editSummary.value = (page.value && page.value.summary) || "";
	editBody.value = (page.value && page.value.body_md) || "";
	editing.value = true;
}
function cancelEdit() {
	editing.value = false;
}

async function save() {
	saving.value = true;
	try {
		await saveWikiPage(props.slug, {
			title: editTitle.value.trim() || undefined,
			summary: editSummary.value,
			body_md: editBody.value,
		});
		// a saved body counts as a review server-side - mirror that locally
		if (page.value) {
			if (editTitle.value.trim()) page.value.title = editTitle.value.trim();
			page.value.summary = editSummary.value;
			page.value.body_md = editBody.value;
			page.value.contradiction_flag = 0;
			page.value.stale = false;
		}
		editing.value = false;
		toast.success("Page saved");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

function confirmArchive() {
	confirmDialog({
		title: "Archive this page?",
		message:
			"Archived pages stop appearing in the list and are no longer used as chat context. The record is kept.",
		onConfirm: async ({ hideDialog }) => {
			archiving.value = true;
			try {
				await archiveWikiPage(props.slug);
				hideDialog();
				if (page.value) page.value.status = "Archived";
				toast.success("Page archived");
			} catch (e) {
				toast.error(errHtml(e));
			} finally {
				archiving.value = false;
			}
		},
	});
}

// no confirm: Restore is itself the escape hatch for an accidental archive
async function restore() {
	archiving.value = true;
	try {
		await restoreWikiPage(props.slug);
		if (page.value) page.value.status = "Active";
		toast.success("Page restored");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		archiving.value = false;
	}
}

function confirmDelete() {
	confirmDialog({
		title: "Delete this page?",
		message: "Permanently deletes this page - archiving keeps it recoverable.",
		onConfirm: async ({ hideDialog }) => {
			deleting.value = true;
			try {
				await deleteWikiPage(props.slug);
				hideDialog();
				toast.success("Page deleted");
				bypassGuard = true;
				router.push({ name: "SkillsList", hash: "#wiki" });
			} catch (e) {
				toast.error(errHtml(e));
			} finally {
				deleting.value = false;
			}
		},
	});
}

const overflowOptions = computed(() => {
	if (!page.value) return [];
	const opts = [];
	if (canPromote.value && !promoPending.value)
		opts.push({ label: "Request promotion…", onClick: () => (promoDialog.value = true) });
	if (page.value.can_archive && page.value.status !== "Archived")
		opts.push({ label: "Archive", onClick: () => confirmArchive() });
	if (page.value.can_archive && page.value.status === "Archived")
		opts.push({ label: "Restore", onClick: () => restore() });
	if (page.value.can_archive) opts.push({ label: "Delete", onClick: () => confirmDelete() });
	return opts;
});

// ── load / init (re-runs when /skills/wiki/new saves and replaces to
// /skills/wiki/:slug, the SkillDetail precedent) ─────────────────────────────
let bypassGuard = false;

async function init() {
	bypassGuard = false;
	loadError.value = "";
	editing.value = false;
	previewing.value = false;
	myPromo.value = null;
	page.value = null;
	if (props.isNew) {
		loading.value = true;
		const ok = await loadCaps();
		loading.value = false;
		if (!ok) {
			loadError.value = "Could not load page options. Reload to try again.";
			return;
		}
		if (!caps.creatable_scopes.length) {
			loadError.value = "You don't have permission to create wiki pages.";
			return;
		}
		resetCreateForm();
		return;
	}
	if (!props.slug) return;
	loading.value = true;
	try {
		page.value = await getWikiPage(props.slug);
		loadMyPromo();
	} catch (e) {
		loadError.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}
watch(() => [props.slug, props.isNew], init, { immediate: true });

// ── dirty guard (D21, the SkillDetail/MacroDetail precedent) ────────────────
const dirty = computed(() => {
	if (props.isNew)
		return !!(
			form.title.trim() ||
			form.page_type ||
			form.summary.trim() ||
			form.body_md.trim()
		);
	if (!editing.value) return false;
	return (
		editTitle.value !== ((page.value && page.value.title) || "") ||
		editSummary.value !== ((page.value && page.value.summary) || "") ||
		editBody.value !== ((page.value && page.value.body_md) || "")
	);
});

onBeforeRouteLeave((to, from, next) => {
	if (bypassGuard || !dirty.value) return next();
	let decided = false;
	confirmDialog({
		title: "Discard unsaved changes?",
		message: props.isNew
			? "This new page hasn't been saved and will be lost."
			: "Your edits to this page will be lost.",
		onConfirm: ({ hideDialog }) => {
			decided = true;
			hideDialog();
			next();
		},
		onCancel: () => {
			if (!decided) next(false);
		},
	});
});
</script>
