<template>
	<Dialog v-model="show" :options="{ title: (page && page.title) || 'Wiki page', size: 'xl' }">
		<template #body-content>
			<div v-if="loading" class="py-8 text-center">
				<JvSpinner />
			</div>
			<template v-else-if="page">
				<!-- metadata row: type · scope (+target) · slug · updated · flags -->
				<div class="flex flex-wrap items-center gap-2 text-sm">
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
						<span class="text-ink-gray-5">· updated {{ timeAgo(updatedAt) }}</span>
					</Tooltip>
					<Badge
						v-if="page.contradiction_flag"
						variant="subtle"
						theme="red"
						label="Conflicting"
					/>
					<Badge v-if="page.stale" variant="subtle" theme="orange" label="Stale" />
					<PromotionStatusChip v-if="myPromo" :req="myPromo" noun="page" />
				</div>

				<!-- What this page documents: a deep link to the ERP record it's about. -->
				<div v-if="refUrl" class="mt-2 flex items-center gap-1.5 text-sm text-ink-gray-5">
					<span>About</span>
					<a
						:href="refUrl"
						target="_blank"
						rel="noopener"
						class="flex min-w-0 items-center gap-1 text-ink-gray-8 hover:underline"
					>
						<span class="truncate">{{ page.ref_doctype }} · {{ page.ref_name }}</span>
						<FeatherIcon
							name="external-link"
							class="size-3.5 shrink-0 text-ink-gray-5"
						/>
					</a>
				</div>

				<!-- Conflicting gets the same treatment as Stale: a badge alone is
				     a red dead-end with no explanation or resolution path -->
				<div
					v-if="page.contradiction_flag"
					class="mt-3 rounded-lg border border-outline-red-2 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
				>
					People have reported conflicting facts - look for the "Contradiction flagged"
					section in the body below.{{
						page.can_edit
							? " Edit the page to keep the correct version; saving marks the conflict resolved."
							: " A wiki manager can edit the page to resolve it."
					}}
				</div>
				<!-- mirrors the amber Stale badge on the list row -->
				<div
					v-if="page.stale"
					class="mt-3 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
				>
					Not confirmed in 90+ days{{
						page.can_edit ? " - saving an edit marks it reviewed." : "."
					}}
				</div>

				<template v-if="editing">
					<FormControl
						type="text"
						class="mt-3"
						label="Title"
						:modelValue="editTitle"
						@update:modelValue="(v) => (editTitle = v)"
					/>
					<FormControl
						type="textarea"
						class="mt-3"
						label="Summary"
						:rows="2"
						:modelValue="editSummary"
						@update:modelValue="(v) => (editSummary = v)"
					/>
					<!-- lg+: textarea and live preview side by side (preview tracks
					     editBody as you type); below lg the Preview button toggles
					     between the two, exactly as before -->
					<div class="mt-3 grid gap-4 lg:grid-cols-2">
						<FormControl
							type="textarea"
							:class="previewing ? 'hidden lg:block' : ''"
							label="Body (markdown)"
							:rows="14"
							:modelValue="editBody"
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
						class="mt-2 lg:hidden"
						variant="ghost"
						:label="previewing ? 'Back to editing' : 'Preview'"
						:iconLeft="previewing ? 'edit-2' : 'eye'"
						@click="previewing = !previewing"
					/>
				</template>
				<template v-else>
					<p v-if="page.summary" class="mt-3 text-sm text-ink-gray-6">
						{{ page.summary }}
					</p>
					<!-- renderMarkdown from @/markdown (escapes HTML first - safe) -->
					<div
						v-if="page.body_md"
						class="prose prose-sm mt-3 max-w-none"
						v-html="bodyHtml"
					/>
					<p v-else class="mt-3 text-sm text-ink-gray-5">No content yet.</p>
					<!-- provenance: where Jarvis learned this - earns trust and edits -->
					<p v-if="provenance" class="mt-3 border-t pt-2 text-p-sm text-ink-gray-5">
						{{ provenance }}
					</p>
				</template>
			</template>
		</template>
		<template #actions>
			<div v-if="page" class="flex flex-wrap items-center gap-2">
				<template v-if="editing">
					<Button variant="solid" label="Save" :loading="saving" @click="save" />
					<Button label="Cancel" @click="editing = false" />
				</template>
				<template v-else>
					<Button
						v-if="page.can_edit"
						variant="subtle"
						label="Edit"
						iconLeft="edit-2"
						@click="startEdit"
					/>
					<Button
						v-if="canPromote && !promoPending"
						variant="subtle"
						label="Request promotion…"
						iconLeft="arrow-up-circle"
						@click="promoDialog = true"
					/>
					<Button
						v-if="page.can_archive && page.status !== 'Archived'"
						variant="subtle"
						theme="red"
						label="Archive"
						:loading="archiving"
						@click="confirmArchive"
					/>
					<Button
						v-if="page.can_archive && page.status === 'Archived'"
						variant="subtle"
						label="Restore"
						iconLeft="rotate-ccw"
						:loading="archiving"
						@click="restore"
					/>
					<Button
						v-if="page.can_archive"
						variant="ghost"
						theme="red"
						label="Delete"
						iconLeft="trash-2"
						:loading="deleting"
						@click="confirmDelete"
					/>
				</template>
			</div>
		</template>
	</Dialog>

	<PromotionRequestDialog
		v-model="promoDialog"
		noun="page"
		:busy="promoBusy"
		@submit="submitPromotion"
	/>
</template>

<script setup>
// WikiPageDialog - THE full-featured wiki editor popup (size xl): rendered-
// markdown read view with scope/attention metadata + provenance, and an Edit
// mode (title + summary + body) shown only when the server-computed `can_edit`
// flag allows it. Editing shows the textarea and a live rendered preview side
// by side at lg+ (below lg a Preview button toggles between them). Archive and
// permanent Delete each sit behind a confirmDialog when `can_archive`. Fetches
// the page itself whenever it opens (v-model true + slug); emits `refresh`
// after a save/archive/delete so the owning list refetches.
import { ref, computed, watch } from "vue";
import {
	Badge,
	Button,
	Dialog,
	FeatherIcon,
	FormControl,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import { renderMarkdown } from "@/markdown";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	getWikiPage,
	saveWikiPage,
	archiveWikiPage,
	restoreWikiPage,
	deleteWikiPage,
	requestWikiPromotion,
	myWikiPromotion,
} from "@/api/wiki";
import JvSpinner from "@/components/JvSpinner.vue";
import PromotionRequestDialog from "@/components/skills/PromotionRequestDialog.vue";
import PromotionStatusChip from "@/components/skills/PromotionStatusChip.vue";
import { errHtml } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	slug: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "refresh"]);

const SCOPE_THEME = { Org: "gray", Role: "blue", User: "green" };

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const page = ref(null);
const loading = ref(false);
const editing = ref(false);
const previewing = ref(false);

// ── promotion (requester side, Skills-area promotion surfacing) ───────────────
// The wiki requester side was never wired: request_wiki_promotion existed but had
// no caller, so the reviewer queue consumed requests nothing could create. Here
// the owner of a private (User-scope) page can finally file one, into the SAME
// reviewer queue. A User-scope page is editable only by its owner, so can_edit on
// a User page ⟺ "mine".
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
	try {
		const res = await myWikiPromotion(props.slug);
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
		toast.success("Promotion requested — a reviewer will decide.");
		await loadMyPromo();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		promoBusy.value = false;
	}
}
const editTitle = ref("");
const editSummary = ref("");
const editBody = ref("");
const saving = ref(false);
const archiving = ref(false);
const deleting = ref(false);

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
// The ERP record this page documents (ref_doctype/ref_name) as a Desk deep link -
// same doctype-route transform as ActivityDetailDialog's target link.
const refUrl = computed(() => {
	if (!page.value || !page.value.ref_doctype || !page.value.ref_name) return "";
	const dt = page.value.ref_doctype.toLowerCase().replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(page.value.ref_name)}`;
});
// "From a voice note by X, Jul 7" - the page's latest source entry. Pages a
// pipeline wrote (not a person) earn trust by saying where they came from.
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

watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
	}
);

async function load() {
	page.value = null;
	myPromo.value = null;
	editing.value = false;
	loading.value = true;
	try {
		page.value = await getWikiPage(props.slug);
		loadMyPromo();
	} catch (e) {
		show.value = false;
		toast.error(errHtml(e));
	} finally {
		loading.value = false;
	}
}

function startEdit() {
	previewing.value = false;
	editTitle.value = (page.value && page.value.title) || "";
	editSummary.value = (page.value && page.value.summary) || "";
	editBody.value = (page.value && page.value.body_md) || "";
	editing.value = true;
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
		emit("refresh");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

async function restore() {
	archiving.value = true;
	try {
		await restoreWikiPage(props.slug);
		if (page.value) page.value.status = "Active";
		toast.success("Page restored");
		emit("refresh");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		archiving.value = false;
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
				show.value = false;
				toast.success("Page archived");
				emit("refresh");
			} catch (e) {
				toast.error(errHtml(e));
			} finally {
				archiving.value = false;
			}
		},
	});
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
				show.value = false;
				toast.success("Page deleted");
				emit("refresh");
			} catch (e) {
				toast.error(errHtml(e));
			} finally {
				deleting.value = false;
			}
		},
	});
}
</script>
