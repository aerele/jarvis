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

			<div v-if="!rec.can_act" class="mt-3 text-sm text-ink-gray-6" role="status">
				{{ closedText }}
			</div>

			<template v-else>
				<div class="mt-4 flex flex-wrap items-center gap-2">
					<Button
						variant="solid"
						label="Create & continue"
						:loading="busy === 'create'"
						:disabled="busy !== null"
						@click="decide('create')"
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
import { getPendingAction, decideHeldAction } from "@/api/approvals";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";
import { errMessage, escapeHtml } from "@/lib/errors";
import {
	filesWaiting,
	isSettled,
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

onMounted(load);
onBeforeUnmount(() => linkSearch.cleanup());
</script>
