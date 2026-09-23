<template>
	<!-- One held File Box write, expanded under its lane row. The card is the
	     server-built "what will be created" summary rendered by PendingCard (text
	     interpolation only - the values are model-derived from a dropped file). -->
	<div
		class="border-t bg-surface-white px-5 py-4"
		:aria-busy="loading || busy !== null ? 'true' : 'false'"
	>
		<div v-if="loading" class="flex justify-start">
			<JvSpinner label="Loading the proposed record…" />
		</div>

		<div v-else-if="loadError" role="alert" class="flex items-center gap-3 text-sm">
			<span class="text-ink-red-5">{{ loadError }}</span>
			<Button variant="subtle" size="sm" label="Try again" @click="load" />
		</div>

		<template v-else-if="rec">
			<p class="text-sm text-ink-gray-6">
				A File Box run wants to create this and is paused until you decide.
				<span class="font-medium text-ink-gray-8">{{ waitingText }}</span>
				<template v-if="rec.for_user"> · dropped by {{ rec.for_user }}</template>
			</p>

			<div class="mt-3">
				<PendingCard v-if="card" :card="card" details="" />
				<div v-else class="text-base text-ink-gray-8">{{ rec.summary }}</div>
			</div>
			<p
				v-if="missingLabels.length && rec.can_act"
				class="mt-2 rounded bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
			>
				{{ __("Missing: {0}", [missingText]) }}
			</p>

			<div v-if="!rec.can_act" class="mt-3 text-sm text-ink-gray-6" role="status">
				{{ closedText }}
			</div>

			<section
				v-else-if="editing"
				class="mt-4 flex flex-col gap-3"
				:aria-label="__('Edit & create')"
				:aria-busy="formLoading ? 'true' : 'false'"
			>
				<JvSpinner v-if="formLoading" :label="__('Loading the form…')" />
				<template v-else>
					<div v-for="(f, i) in forms" :key="i" class="rounded-lg border p-3">
						<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
							<h3 class="text-sm font-medium text-ink-gray-8">
								{{
									forms.length > 1
										? __("Record {0}: {1}", [i + 1, f.doctype])
										: f.doctype
								}}
							</h3>
							<a
								:href="deskNewUrl(f.doctype, f.values, f.secret)"
								target="_blank"
								rel="noopener"
								class="text-sm text-ink-gray-6 underline"
								>{{ __("Open full form in Desk") }}</a
							>
						</div>
						<DocFieldsForm
							v-if="f.meta"
							:meta="f.meta"
							:values="f.values"
							:needs-input="f.needs"
							:locked="f.locked"
							:errors="f.errors"
							@update:values="(v) => (f.values = v)"
						/>
						<p v-else role="alert" class="text-sm text-ink-red-5">
							{{
								__(
									"This form couldn't be loaded. Create it in the full form in Desk, then choose Use existing."
								)
							}}
						</p>
					</div>
				</template>
				<div
					v-if="banner"
					role="alert"
					class="rounded bg-surface-red-1 px-3 py-2 text-sm text-ink-red-5"
				>
					{{ banner }}
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<Button
						variant="solid"
						:label="__('Create & continue')"
						:loading="busy === 'edit'"
						:disabled="busy !== null || formLoading || editBlocked || !!ended"
						:aria-describedby="editBlocked ? editReasonId : undefined"
						@click="submitEdit"
					/>
					<Button
						variant="ghost"
						:label="ended ? __('Close') : __('Cancel')"
						:disabled="busy !== null"
						@click="ended ? emit('decided', ended) : (editing = false)"
					/>
				</div>
				<p v-if="editBlocked" :id="editReasonId" class="text-sm text-ink-amber-3">
					{{ __("Fill {0} first.", [editMissingText]) }}
				</p>
			</section>

			<template v-else>
				<div class="mt-4 flex flex-wrap items-center gap-2">
					<Button
						variant="solid"
						label="Create & continue"
						:loading="busy === 'create'"
						:disabled="busy !== null || blocked"
						:aria-describedby="blocked ? reasonId : undefined"
						@click="decide('create')"
					/>
					<Button
						v-if="rec.can_edit"
						variant="subtle"
						:label="__('Edit & create')"
						:disabled="busy !== null"
						@click="startEdit"
					/>
					<Button
						v-if="rec.can_use_existing"
						variant="subtle"
						label="Use existing"
						:aria-expanded="showExisting ? 'true' : 'false'"
						:aria-controls="existingId"
						:disabled="busy !== null"
						@click="toggleExisting"
					/>
					<Button
						variant="ghost"
						theme="red"
						:label="skipText"
						:loading="busy === 'skip'"
						:disabled="busy !== null"
						@click="decide('skip')"
					/>
				</div>
				<p v-if="blocked" :id="reasonId" class="mt-2 text-sm text-ink-amber-3">
					{{ __("Fill {0} — use Edit & create.", [missingText]) }}
				</p>
				<div v-if="notice" role="alert" class="mt-2 text-sm text-ink-red-5">
					{{ notice }}
				</div>

				<div
					v-if="showExisting"
					:id="existingId"
					ref="existingEl"
					tabindex="-1"
					class="mt-3 rounded-lg border p-3 outline-none"
					:aria-labelledby="existingId + '-title'"
				>
					<div :id="existingId + '-title'" class="text-sm font-medium text-ink-gray-8">
						Use an existing {{ rec.doctype }} instead
					</div>
					<ul v-if="rec.candidates.length" class="mt-2 flex flex-col divide-y">
						<li
							v-for="c in rec.candidates"
							:key="c.name"
							class="flex items-center gap-3 py-2"
						>
							<div class="min-w-0 flex-1">
								<div class="truncate text-base text-ink-gray-9">
									{{ c.title || c.name }}
								</div>
								<div class="truncate text-sm text-ink-gray-5">
									{{ c.name
									}}<template v-if="c.gstin"> · {{ c.gstin }}</template>
								</div>
							</div>
							<Button
								variant="subtle"
								size="sm"
								label="Use this"
								:aria-label="'Use ' + (c.title || c.name)"
								:loading="busy === 'use_existing:' + c.name"
								:disabled="busy !== null"
								@click="decide('use_existing', c.name)"
							/>
						</li>
					</ul>
					<div v-else class="mt-2 text-sm text-ink-gray-5">
						No matching {{ rec.doctype }} found. Search for one below.
					</div>
					<div class="mt-3" role="group" :aria-label="'Search ' + rec.doctype">
						<Autocomplete
							:options="linkSearch.options.value"
							:loading="linkSearch.loading.value"
							:modelValue="null"
							:placeholder="'Search ' + rec.doctype + '…'"
							@update:query="linkSearch.onQuery"
							@update:modelValue="(o) => o && decide('use_existing', o.value)"
						/>
					</div>
				</div>
			</template>
		</template>
	</div>
</template>

<script setup>
// Detail + decision for one held File Box write (PR-2b). Create & continue runs
// the sealed call as the person who dropped the file; Use existing resolves it
// with a record they can read; Skip discards it. The server re-checks everything
// and answers with a reason_code; the words live in lib/heldActions.
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from "vue";
import { Autocomplete, Button, toast } from "frappe-ui";
import PendingCard from "@/components/PendingCard.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import DocFieldsForm from "@/components/forms/DocFieldsForm.vue";
import { getPendingAction, decideHeldAction, editAndCreateHeld } from "@/api/approvals";
import { getDoctypeFormMeta, searchLink } from "@/api";
import { deskNewUrl, errorsByKey, patchOf, stillMissing } from "@/lib/heldEdit";
import { __ } from "@/lib/i18n";
import { useLinkSearch } from "@/composables/useLinkSearch";
import { errMessage, escapeHtml } from "@/lib/errors";
import {
	filesWaiting,
	isSettled,
	missingSummary,
	outcomeMessage,
	refusalMessage,
	skipLabel,
	statusLine,
} from "@/lib/heldActions";

const props = defineProps({
	name: { type: String, required: true },
});
const emit = defineEmits(["decided"]);

const rec = ref(null);
const loading = ref(true);
const loadError = ref("");
const busy = ref(null); // "create" | "skip" | "use_existing:<name>" | null
const notice = ref("");
const showExisting = ref(false);
const existingEl = ref(null);
const existingId = computed(() => "held-existing-" + props.name);

// The card must be a plain object with a kind to render; anything else falls back
// to the summary line (never a raw dump).
const card = computed(() => {
	const c = rec.value && rec.value.card;
	return c && typeof c === "object" && c.kind ? c : null;
});
const waitingText = computed(() => filesWaiting(rec.value && rec.value.waiters_count));
const skipText = computed(() => skipLabel(rec.value && rec.value.waiters_count));
const closedText = computed(() => statusLine(rec.value));

const linkSearch = useLinkSearch((q) => searchLink(rec.value.doctype, q, 10), {
	mapper: (rows) =>
		rows.map((r) => ({
			label: r.label || r.value,
			value: String(r.value),
			description: r.label && r.label !== r.value ? String(r.value) : r.description || "",
		})),
});

let req = 0; // monotonic: a stale load never overwrites a newer one
async function load() {
	const id = ++req;
	loading.value = !rec.value;
	loadError.value = "";
	try {
		const res = await getPendingAction(props.name);
		if (id !== req) return;
		rec.value = res || null;
	} catch (e) {
		if (id !== req) return;
		loadError.value = errMessage(e, "This approval could not be loaded.");
	} finally {
		if (id === req) loading.value = false;
	}
}

async function focusExisting() {
	showExisting.value = true;
	await nextTick();
	if (existingEl.value && existingEl.value.focus) existingEl.value.focus();
}

function toggleExisting() {
	if (showExisting.value) showExisting.value = false;
	else focusExisting();
}

async function decide(action, useExisting) {
	if (busy.value !== null || !rec.value) return;
	busy.value = action === "use_existing" ? "use_existing:" + useExisting : action;
	notice.value = "";
	try {
		const res = (await decideHeldAction(props.name, action, useExisting)) || {};
		if (res.ok) {
			toast.success(escapeHtml(outcomeMessage(res)));
			emit("decided", res);
			return;
		}
		const message = refusalMessage(res);
		if (isSettled(res)) {
			toast.error(escapeHtml(message));
			emit("decided", res);
			return;
		}
		notice.value = message;
		if (res.reason_code === "exists") {
			// A match appeared since this was held: refresh the candidates and hand
			// the approver straight to Use existing.
			await load();
			if (rec.value && rec.value.can_use_existing) await focusExisting();
		}
	} catch (e) {
		notice.value = errMessage(e);
	} finally {
		busy.value = null;
	}
}

// --- Missing fields + Edit & create (PR-2d) ---
const reasonId = computed(() => "held-reason-" + props.name);
const editReasonId = computed(() => "held-edit-reason-" + props.name);
const needsInput = computed(() => (rec.value && rec.value.needs_input) || []);
const missingLabels = computed(() => needsInput.value.map((e) => e.label));
const missingText = computed(() => missingSummary(missingLabels.value));
const blocked = computed(() => missingLabels.value.length > 0);

const editing = ref(false);
const formLoading = ref(false);
const forms = ref([]); // per record: {doctype, meta, original, values, needs, locked, secret, errors}
const banner = ref("");
const ended = ref(null); // a terminal failure: the values stay on screen until Close
const metaCache = {};

const editMissing = computed(() =>
	forms.value.flatMap((f, i) => stillMissing(needsInput.value, i, f.values))
);
const editBlocked = computed(() => editMissing.value.length > 0);
const editMissingText = computed(() => missingSummary(editMissing.value.map((e) => e.label)));

async function formMeta(doctype) {
	if (!metaCache[doctype]) {
		const res = await getDoctypeFormMeta(doctype);
		if (!res || !res.ok) throw new Error("no form meta");
		metaCache[doctype] = res;
	}
	return metaCache[doctype];
}

async function startEdit() {
	if (!rec.value || busy.value !== null) return;
	editing.value = true;
	banner.value = "";
	ended.value = null;
	formLoading.value = true;
	const hidden = __("Hidden: a secret value.");
	forms.value = (rec.value.docs || []).map((d, i) => ({
		doctype: d.doctype,
		meta: null,
		original: d.values || {},
		values: { ...(d.values || {}) },
		needs: needsInput.value.filter((e) => (e.doc_index || 0) === i),
		locked: {
			...(d.locked || {}),
			...Object.fromEntries((d.secret || []).map((k) => [k, hidden])),
		},
		secret: d.secret || [],
		errors: {},
	}));
	await Promise.all(
		forms.value.map(async (f) => {
			try {
				f.meta = await formMeta(f.doctype);
			} catch {
				f.meta = null;
			}
		})
	);
	formLoading.value = false;
}

async function submitEdit() {
	if (busy.value !== null || editBlocked.value || ended.value) return;
	busy.value = "edit";
	banner.value = "";
	forms.value.forEach((f) => (f.errors = {}));
	try {
		const patches = forms.value.map((f) => patchOf(f.original, f.values));
		const res = (await editAndCreateHeld(props.name, patches)) || {};
		if (res.ok) {
			toast.success(escapeHtml(outcomeMessage(res)));
			emit("decided", res);
			return;
		}
		if (res.reason_code === "exists") {
			// The party exists now: back to the decisions, straight to Use existing.
			editing.value = false;
			notice.value = refusalMessage(res);
			await load();
			if (rec.value && rec.value.can_use_existing) await focusExisting();
			return;
		}
		forms.value.forEach((f, i) => (f.errors = errorsByKey(res.errors, i)));
		banner.value = refusalMessage(res);
		if (isSettled(res)) ended.value = res; // Failed after the claim: keep the values
	} catch (e) {
		banner.value = errMessage(e);
	} finally {
		busy.value = null;
	}
}

onMounted(load);
onBeforeUnmount(() => linkSearch.cleanup());
</script>
