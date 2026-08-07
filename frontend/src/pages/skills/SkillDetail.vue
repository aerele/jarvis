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
			<SyncPill ref="syncPill" />
			<PromotionStatusChip v-if="canEdit && myPromo" :req="myPromo" noun="skill" />
			<Badge
				v-if="skill && !canEdit"
				variant="subtle"
				theme="gray"
				size="lg"
				:label="`Shared by ${sharedBy || 'another user'} · read-only`"
			/>
			<Dropdown v-if="canEdit && !isNew" :options="overflowOptions">
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
			<Button
				v-if="canEdit"
				variant="solid"
				label="Save"
				:disabled="!dirty"
				:loading="saving"
				@click="save"
			/>
		</template>

		<template #main>
			<DocSection label="Details">
				<div class="space-y-4">
					<FormControl
						type="text"
						label="Name"
						required
						placeholder="e.g. monthly-close"
						:modelValue="form.skill_name"
						:disabled="!isNew || saving"
						:description="
							isNew
								? `Lowercase letters, digits and hyphens - trigger it in chat with /${
										form.skill_name || 'name'
								  }.`
								: 'Skill names can\'t be changed after creation.'
						"
						@update:modelValue="(v) => (form.skill_name = v)"
					/>
					<FormControl
						type="textarea"
						label="Description"
						required
						:rows="2"
						placeholder="When should the assistant use this skill?"
						description="A short hint so the assistant knows when this skill applies."
						:modelValue="form.description"
						:disabled="readonly"
						@update:modelValue="(v) => (form.description = v)"
					/>
					<Switch
						v-model="form.enabled"
						label="Enabled"
						description="Off = saved as a draft, not used by the assistant."
						:disabled="readonly"
					/>
					<Switch
						v-model="form.user_invocable"
						label="User invocable"
						description="Show in the chat / menu so users can trigger it directly."
						:disabled="readonly"
					/>
				</div>
			</DocSection>

			<DocSection label="Instructions">
				<template #header-suffix>
					<span class="text-ink-red-3 select-none" aria-hidden="true">*</span>
					<span class="sr-only">(required)</span>
				</template>
				<!-- lg+: textarea and live rendered preview side by side (preview
				     tracks form.instructions as you type); below lg a Preview button
				     toggles between them - exactly WikiPageDialog.vue:80-107's pattern,
				     replicated here for the Instructions markdown (goal item 1). -->
				<div class="grid gap-4 lg:grid-cols-2">
					<FormControl
						type="textarea"
						:rows="14"
						:class="[
							'font-mono [&_textarea]:min-h-[320px]',
							previewingInstructions ? 'hidden lg:block' : '',
						]"
						placeholder="Markdown instructions the assistant follows when this skill runs…"
						description="Markdown instructions the model follows when this skill runs."
						:modelValue="form.instructions"
						:disabled="readonly"
						@update:modelValue="(v) => (form.instructions = v)"
					/>
					<div :class="previewingInstructions ? '' : 'hidden lg:block'">
						<div class="text-xs text-ink-gray-5">Preview</div>
						<div
							class="prose prose-sm mt-1 min-h-[320px] max-w-none overflow-y-auto rounded border px-3 py-2"
							v-html="instructionsPreviewHtml"
						/>
					</div>
				</div>
				<Button
					class="mt-2 lg:hidden"
					variant="ghost"
					:label="previewingInstructions ? 'Back to editing' : 'Preview'"
					:iconLeft="previewingInstructions ? 'edit-2' : 'eye'"
					@click="previewingInstructions = !previewingInstructions"
				/>
			</DocSection>
		</template>

		<template #aside>
			<!-- new-record mode: metadata exists only after the first save -->
			<div v-if="isNew" class="m-5 rounded-md border p-4 text-sm text-ink-gray-6">
				Save to enable comments, attachments and sharing.
			</div>

			<DocMetaPanel v-else-if="canEdit && docmeta" :docmeta="docmeta" :can-write="true">
				<!-- skills keep the child-table share model (§14 F1/DA-09) -->
				<template #extra>
					<div class="px-5 py-4">
						<div class="flex items-center justify-between">
							<div class="text-sm text-ink-gray-5">Shared with</div>
							<Button label="Manage" @click="shareOpen = true" />
						</div>
						<div v-if="shares.length" class="mt-2 flex items-center">
							<Tooltip
								v-for="u in shares.slice(0, 8)"
								:key="u.name"
								:text="u.full_name || u.name"
							>
								<Avatar
									size="md"
									:label="u.full_name || u.name"
									class="-mr-1.5 ring-2 ring-outline-white hover:z-10 hover:scale-110"
								/>
							</Tooltip>
							<span v-if="shares.length > 8" class="ml-3 text-sm text-ink-gray-5">
								+{{ shares.length - 8 }}
							</span>
						</div>
						<div v-else class="mt-2 text-sm text-ink-gray-4">Not shared</div>
					</div>
				</template>
			</DocMetaPanel>

			<!-- shared-with-me: read-only info card (no docmeta - the docmeta read
			     gate covers owner/SM/DocShare only; child-table shares would 403) -->
			<div v-else-if="skill && !canEdit" class="m-5 space-y-3 rounded-md border p-4">
				<div class="flex items-center gap-2">
					<Avatar size="md" :label="sharedBy || 'another user'" />
					<div class="text-base font-medium text-ink-gray-8">
						{{ sharedBy || "Another user" }}
					</div>
				</div>
				<div class="text-sm text-ink-gray-5">
					Shared with you · read-only. Use /{{ form.skill_name }} in chat - you can’t
					edit or re-share it.
				</div>
				<div v-if="skill.modified" class="text-sm text-ink-gray-5">
					Updated {{ timeAgo(skill.modified) }}
				</div>
			</div>
		</template>

		<template #footer>
			<CommentsSection
				v-if="!isNew && canEdit && docmeta"
				:docmeta="docmeta"
				:can-comment="true"
			/>
		</template>
	</DocPage>

	<ShareDialog
		v-model="shareOpen"
		:skill="{ name: id, skill_name: form.skill_name }"
		@saved="loadShares"
	/>

	<PromotionRequestDialog
		v-model="promoDialog"
		noun="skill"
		:busy="promoBusy"
		@submit="submitPromotion"
	/>

	<!-- Discard-changes confirm — styled with the chat SPA's jv-* design system
	     (palette vars + overlay + jv-btn) to match the Settings dialog. -->
	<div
		v-if="discardOpen"
		class="jv-settings-overlay jv-root"
		:class="{ 'jv-dark': dark }"
		:style="paletteVars"
		@click.self="resolveDiscard(false)"
		@keydown.esc="resolveDiscard(false)"
	>
		<div
			class="jv-confirm"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="discard-title"
		>
			<div class="jv-confirm-head">
				<span class="jv-confirm-icon" aria-hidden="true">
					<svg
						width="17"
						height="17"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
						/>
						<line x1="12" y1="9" x2="12" y2="13" />
						<line x1="12" y1="17" x2="12.01" y2="17" />
					</svg>
				</span>
				<h2 id="discard-title" class="jv-confirm-title">Discard unsaved changes?</h2>
			</div>
			<p class="jv-confirm-msg">
				Your edits to this skill haven’t been saved and will be lost.
			</p>
			<div class="jv-confirm-actions">
				<button
					ref="cancelBtn"
					type="button"
					class="jv-btn jv-btn--ghost"
					@click="resolveDiscard(false)"
				>
					Cancel
				</button>
				<button type="button" class="jv-btn jv-btn--danger" @click="resolveDiscard(true)">
					Discard changes
				</button>
			</div>
		</div>
	</div>
</template>

<script setup>
// Skill detail + create page (DESIGN-V3 §6.2): /skills/:id and /skills/new
// share this component (isNew prop). Explicit Save (D21) with dirty guard,
// read-only mode for shared-with-me skills, DocMetaPanel + child-table
// Shared-with block (#extra) + ShareDialog, sync pill on save/delete.
import { ref, reactive, computed, watch, shallowRef, nextTick, onMounted } from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import {
	Button,
	Badge,
	Avatar,
	Tooltip,
	Dropdown,
	FormControl,
	Switch,
	toast,
	confirmDialog,
} from "frappe-ui";
import DocPage from "@/components/doc/DocPage.vue";
import DocSection from "@/components/doc/DocSection.vue";
import DocMetaPanel from "@/components/doc/DocMetaPanel.vue";
import CommentsSection from "@/components/doc/CommentsSection.vue";
import { useDocmeta } from "@/composables/useDocmeta";
import SyncPill from "./SyncPill.vue";
import ShareDialog from "./ShareDialog.vue";
import PromotionRequestDialog from "@/components/skills/PromotionRequestDialog.vue";
import PromotionStatusChip from "@/components/skills/PromotionStatusChip.vue";
import { getSkillsAreaCaps } from "@/api/personalise";
import { requestSkillPromotion, mySkillPromotion } from "@/api/skills";
import { renderMarkdown } from "@/markdown";
import { timeAgo } from "@/utils/datetime";
import { useJarvisTheme } from "@/theme";
import "@/assets/settings.css"; // shared jv-* primitives (overlay, jv-btn) for the discard dialog
import * as api from "@/api";
import { errMessage as errMsg, errHtml } from "@/lib/errors";

const props = defineProps({
	id: { type: String, default: "" },
	isNew: { type: Boolean, default: false },
});

const router = useRouter();
// Chat-SPA theme (jv-* palette vars + dark flag) so the discard dialog matches
// the chat surface / Settings dialog rather than frappe-ui's default styling.
const { effectiveDark: dark, paletteVars } = useJarvisTheme();
const DOCTYPE = "Jarvis Custom Skill";
const FIELDS = ["skill_name", "description", "instructions", "user_invocable", "enabled"];

// ── state ────────────────────────────────────────────────────────────────────
const skill = ref(null); // last server copy (null while new)
const loading = ref(false);
const loadError = ref("");
const saving = ref(false);

const form = reactive({
	skill_name: "",
	description: "",
	instructions: "",
	user_invocable: true,
	enabled: true,
});
const snapshot = ref({ ...form }); // saved-state copy (ref so `dirty` recomputes when reset after save)

const docmeta = shallowRef(null); // useDocmeta instance (own skills only)
const shares = ref([]); // [{name, full_name}]
const shareOpen = ref(false);
const syncPill = ref(null);

// Only a reviewer/admin may push the shared container (apply is reviewer-gated -
// security review PART 2 TASK 12). New skills default to private (User) scope
// and are NEVER pushed, so for an ordinary user apply-on-save is both pointless
// and a hard 403 (a scary red toast). Gate every apply on this signal.
const isReviewer = ref(false);
onMounted(async () => {
	try {
		const caps = await getSkillsAreaCaps();
		isReviewer.value = !!(caps && caps.review);
	} catch {
		isReviewer.value = false;
	}
});

const canEdit = computed(() => props.isNew || !!(skill.value && skill.value.can_edit));
const readonly = computed(() => !canEdit.value || saving.value);
const sharedBy = computed(() => (skill.value && skill.value.shared_by) || "");

// ── promotion (requester side, Skills-area promotion surfacing) ───────────────
// The owner of a private (User-scope) skill can ask a reviewer to widen it to a
// role or the whole org. Learned rows are managed by the board, not promotion.
const myPromo = ref(null); // {} | most-recent request for THIS skill (status chip)
const promoDialog = ref(false);
const promoBusy = ref(false);
const canPromote = computed(
	() =>
		!props.isNew &&
		canEdit.value &&
		!!skill.value &&
		(skill.value.scope || "User") === "User" &&
		!skill.value.managed_by_learning
);
// Offer the action only when there is no request in flight; a Pending request
// shows the status chip instead (a rejected one may be re-requested).
const promoPending = computed(() => !!(myPromo.value && myPromo.value.status === "Pending"));

async function loadMyPromo() {
	myPromo.value = null;
	if (props.isNew || !props.id || !canEdit.value) return;
	try {
		const res = await mySkillPromotion(props.id);
		myPromo.value = res && res.status ? res : null;
	} catch {
		// best-effort chip; a failure must not disturb the page
	}
}

async function submitPromotion({ to_scope, target_role, note }) {
	promoBusy.value = true;
	try {
		await requestSkillPromotion({ name: props.id, to_scope, target_role, note });
		promoDialog.value = false;
		toast.success("Promotion requested — a reviewer will decide.");
		await loadMyPromo();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		promoBusy.value = false;
	}
}

// Instructions markdown preview (goal item 1) - mirrors WikiPageDialog's
// `previewing`/`previewHtml` pair exactly, scoped to this one field.
const previewingInstructions = ref(false);
const instructionsPreviewHtml = computed(() =>
	form.instructions
		? renderMarkdown(form.instructions)
		: '<p class="text-ink-gray-5">Nothing to preview yet.</p>'
);

function normalize(v) {
	if (typeof v === "boolean") return v ? 1 : 0;
	return v == null ? "" : v;
}
const dirty = computed(() => {
	if (!canEdit.value) return false;
	return FIELDS.some((k) => normalize(form[k]) !== normalize(snapshot.value[k]));
});

const pageTitle = computed(() =>
	props.isNew ? form.skill_name || "New Skill" : form.skill_name || props.id
);
const breadcrumbs = computed(() => [
	{ label: "Skills", route: { name: "SkillsList" } },
	props.isNew
		? { label: "New Skill", route: { name: "SkillNew" } }
		: { label: pageTitle.value, route: { name: "SkillDetail", params: { id: props.id } } },
]);

const overflowOptions = computed(() => {
	const opts = [];
	if (canPromote.value && !promoPending.value)
		opts.push({ label: "Request promotion…", onClick: () => (promoDialog.value = true) });
	opts.push({ label: "Delete", onClick: () => confirmDelete() });
	return opts;
});

// ── load / init (re-runs when /skills/new saves and replaces to /skills/:id) ─
function seed(data) {
	form.skill_name = data.skill_name || "";
	form.description = data.description || "";
	form.instructions = data.instructions || "";
	form.user_invocable = !!data.user_invocable;
	form.enabled = !!data.enabled;
	snapshot.value = { ...form };
}

let bypassGuard = false;

async function init() {
	bypassGuard = false;
	loadError.value = "";
	shares.value = [];
	shareOpen.value = false;
	docmeta.value = null;
	if (props.isNew) {
		skill.value = null;
		seed({ user_invocable: 1, enabled: 1 });
		return;
	}
	if (!props.id) return;
	loading.value = true;
	try {
		const full = await api.getCustomSkill(props.id);
		skill.value = full;
		seed(full);
		if (full.can_edit) {
			docmeta.value = useDocmeta(DOCTYPE, props.id);
			// created outside setup (async load / post-create replace) - fetch
			// explicitly rather than relying on the composable's auto-load
			if (docmeta.value && typeof docmeta.value.reload === "function")
				docmeta.value.reload();
			loadShares();
			loadMyPromo();
		}
	} catch (e) {
		loadError.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

watch(() => [props.id, props.isNew], init, { immediate: true });

async function loadShares() {
	if (props.isNew || !props.id || !canEdit.value) return;
	try {
		const res = (await api.getSkillShares(props.id)) || {};
		shares.value = res.users || [];
	} catch (e) {
		// owner-only endpoint; best-effort
	}
}

// ── save / delete ────────────────────────────────────────────────────────────
async function save() {
	if (saving.value || !dirty.value) return;
	const name = (form.skill_name || "").trim().toLowerCase();
	if (!name) {
		toast.error("Skill name is required.");
		return;
	}
	if (!(form.description || "").trim()) {
		toast.error("Description is required.");
		return;
	}
	if (!(form.instructions || "").trim()) {
		toast.error("Instructions are required.");
		return;
	}
	saving.value = true;
	try {
		const payload = {
			skill_name: name,
			description: form.description,
			instructions: form.instructions,
			user_invocable: form.user_invocable ? 1 : 0,
			enabled: form.enabled ? 1 : 0,
		};
		if (props.isNew) {
			const res = (await api.createCustomSkill(payload)) || {};
			const newName = (res.data && res.data.name) || "";
			snapshot.value = { ...form }; // clean before navigating (leave guard)
			// Reviewers only: a new skill is private (User) scope and never pushed,
			// so ordinary users need no apply (and would 403 on the gated endpoint).
			if (isReviewer.value) syncPill.value && syncPill.value.apply();
			toast.success("Saved");
			if (newName) router.replace("/skills/" + newName);
		} else {
			// send only the changed fields
			const changed = {};
			for (const k of FIELDS) {
				if (normalize(form[k]) !== normalize(snapshot.value[k])) changed[k] = payload[k];
			}
			await api.updateCustomSkill({ name: props.id, ...changed });
			snapshot.value = { ...form };
			skill.value = { ...skill.value, ...payload };
			// Reviewers only (see create branch): the shared push is reviewer-gated.
			if (isReviewer.value) syncPill.value && syncPill.value.apply();
			toast.success("Saved");
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

function confirmDelete() {
	confirmDialog({
		title: "Delete skill?",
		message: `Delete “${
			form.skill_name || props.id
		}”? It will be removed from your assistant.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await api.deleteCustomSkill(props.id);
				// reconcile the container only for reviewers (apply is gated); a
				// deleted private skill never affected the shared push anyway.
				if (isReviewer.value) await api.applyCustomSkills().catch(() => {});
				bypassGuard = true;
				hideDialog();
				toast.success("Skill deleted");
				router.push({ name: "SkillsList" });
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

// ── dirty guard (D21) ────────────────────────────────────────────────────────
// Custom discard dialog (instead of frappe-ui confirmDialog) so we get an
// explicit Cancel action alongside Discard, both aligned on one row.
const discardOpen = ref(false);
const cancelBtn = ref(null);
let pendingNext = null;

onBeforeRouteLeave((to, from, next) => {
	if (bypassGuard || !dirty.value) return next();
	pendingNext = next;
	discardOpen.value = true;
	// Focus the safe default so Enter/Esc land on it (Esc bubbles to the overlay).
	nextTick(() => cancelBtn.value?.focus());
});

// Resolve the pending navigation: proceed=true leaves the page, false stays.
// Idempotent — closing via X/backdrop and a button click can't double-resolve.
function resolveDiscard(proceed) {
	discardOpen.value = false;
	const next = pendingNext;
	pendingNext = null;
	if (next) next(proceed ? undefined : false);
}
</script>

<style scoped>
/* Compact confirm panel — mirrors the chat SPA / Settings dialog surface,
   radius, shadow and pop-in. Colours resolve from the jv-* palette vars set
   inline on the overlay root (:style="paletteVars"); jv-popin + the jv-btn
   button styles come from the shared @/assets/settings.css. */
.jv-confirm {
	width: 384px;
	max-width: 100%;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	box-shadow: 0 24px 70px rgba(20, 20, 30, 0.28);
	padding: 20px 20px 16px;
	animation: jv-popin 0.16s ease;
	font-family: "Inter", system-ui, sans-serif;
}
.jv-confirm-head {
	display: flex;
	align-items: center;
	gap: 10px;
	margin-bottom: 11px;
}
.jv-confirm-icon {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: center;
	width: 32px;
	height: 32px;
	border-radius: 9px;
	background: var(--red-bg);
	border: 1px solid var(--red-bd);
	color: var(--red);
}
.jv-confirm-title {
	margin: 0;
	font-size: 15px;
	font-weight: 650;
	color: var(--text);
}
.jv-confirm-msg {
	margin: 0 0 18px;
	font-size: 13px;
	line-height: 1.5;
	color: var(--text-2);
}
.jv-confirm-actions {
	display: flex;
	justify-content: flex-end;
	gap: 8px;
}
</style>
