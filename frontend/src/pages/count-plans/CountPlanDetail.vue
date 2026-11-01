<template>
	<div class="flex h-full min-h-0 flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Physical count plans', route: { name: 'CountPlansList' } },
						{ label: plan?.warehouse || request },
					]"
				/>
			</template>
			<template #right-header>
				<Button icon="refresh-cw" label="Refresh" :loading="loading" @click="load" />
			</template>
		</LayoutHeader>

		<main class="min-h-0 flex-1 overflow-y-auto px-5 py-6" aria-labelledby="count-plan-title">
			<div
				v-if="error && !plan"
				class="mx-auto max-w-4xl py-10 text-sm text-ink-red-4"
				role="alert"
			>
				{{ error }}
			</div>
			<div
				v-else-if="loading && !plan"
				class="mx-auto flex max-w-4xl items-center gap-2 py-10 text-sm text-ink-gray-5"
			>
				<JvSpinner /> Loading count plan…
			</div>
			<div v-else-if="plan" class="mx-auto max-w-4xl">
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div>
						<h1 id="count-plan-title" class="text-xl font-semibold text-ink-gray-9">
							{{ plan.title || plan.warehouse }}
						</h1>
						<p class="mt-1 text-sm text-ink-gray-6">
							{{ plan.company }} · {{ plan.warehouse }}
						</p>
					</div>
					<Badge
						variant="subtle"
						:theme="canRecord ? 'green' : recordPermission ? 'blue' : 'gray'"
						:label="
							canRecord
								? 'Ready to record'
								: recordPermission
								? 'All planned items recorded'
								: 'View only'
						"
					/>
				</div>
				<p class="mt-4 text-sm text-ink-gray-6">
					Record only what you physically observed. Leave an item blank if you did not
					count it. A blank value is not zero. This page creates an immutable observation
					record; it does not adjust stock.
				</p>

				<p
					v-if="message"
					class="mt-4 rounded-lg bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
					role="status"
				>
					{{ message }}
				</p>
				<p v-if="error" class="mt-4 text-sm text-ink-red-4" role="alert">{{ error }}</p>

				<div
					v-if="!recordPermission"
					class="mt-4 rounded-lg border bg-surface-gray-1 px-4 py-3 text-sm text-ink-gray-6"
				>
					You can view this approved plan, but you do not have the current Stock User and
					read permissions that are required to record an observation.
				</div>
				<div
					v-else-if="!remainingLines.length"
					class="mt-4 rounded-lg border bg-surface-gray-1 px-4 py-3 text-sm text-ink-gray-6"
				>
					Every planned item already has a submitted physical-count observation.
				</div>

				<section
					class="mt-6 overflow-hidden rounded-lg border"
					aria-labelledby="count-lines-title"
				>
					<div class="border-b bg-surface-gray-1 px-4 py-3">
						<h2 id="count-lines-title" class="text-base font-medium text-ink-gray-9">
							Planned items
						</h2>
						<p class="mt-1 text-sm text-ink-gray-6">
							Current book quantity is shown for context. It is not a suggested
							physical count.
						</p>
					</div>
					<div class="divide-y">
						<div
							v-for="line in plan.lines"
							:key="line.item_code"
							class="grid gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_11rem_12rem] sm:items-center"
						>
							<div class="min-w-0">
								<div class="truncate text-sm font-medium text-ink-gray-9">
									{{ line.item_code }}
								</div>
								<div class="mt-0.5 text-xs text-ink-gray-5">
									Stock unit: {{ line.stock_uom }}
								</div>
								<div v-if="line.count_name" class="mt-1 text-xs text-ink-gray-6">
									Recorded in {{ line.count_name }}
								</div>
							</div>
							<div class="text-sm text-ink-gray-6">
								<div class="text-xs text-ink-gray-5">Current book quantity</div>
								{{
									line.current_book_qty == null
										? "Not readable"
										: line.current_book_qty
								}}
							</div>
							<div>
								<label
									class="mb-1 block text-xs text-ink-gray-5"
									:for="inputId(line.item_code)"
								>
									Physical count
								</label>
								<input
									:id="inputId(line.item_code)"
									v-model="quantities[line.item_code]"
									type="text"
									inputmode="decimal"
									autocomplete="off"
									placeholder="Leave blank"
									:disabled="
										!canRecord || !!line.count_name || saving || uncertain
									"
									class="w-full rounded border border-outline-gray-2 px-2 py-1.5 text-sm disabled:bg-surface-gray-1"
								/>
							</div>
						</div>
					</div>
				</section>

				<section
					v-if="canRecord"
					class="mt-6 rounded-lg border p-4"
					aria-labelledby="record-count-title"
				>
					<h2 id="record-count-title" class="text-base font-medium text-ink-gray-9">
						Save observation
					</h2>
					<div class="mt-3 max-w-xs">
						<label class="mb-1 block text-xs text-ink-gray-5" for="counted-at"
							>Counted at (your local time)</label
						>
						<input
							id="counted-at"
							v-model="countedAt"
							type="datetime-local"
							:max="nowLocal"
							:disabled="saving || uncertain"
							class="w-full rounded border border-outline-gray-2 px-2 py-1.5 text-sm disabled:bg-surface-gray-1"
						/>
					</div>
					<p class="mt-1 text-xs text-ink-gray-5">
						Jarvis saves this as
						{{ siteTimeZone ? siteTimeZone + " site time" : "site time" }}.
					</p>
					<p class="mt-3 text-sm text-ink-gray-6">
						{{ entries.length }} item{{ entries.length === 1 ? "" : "s" }} ready to
						save.
					</p>
					<div class="mt-4 flex flex-wrap items-center gap-2">
						<Button
							variant="solid"
							label="Save physical count"
							:loading="saving"
							:disabled="!entries.length || !countedAt || uncertain"
							@click="save"
						/>
						<Button
							v-if="uncertain"
							variant="solid"
							label="Retry same save"
							:loading="saving"
							@click="retry"
						/>
						<Button
							v-if="hasRecordedLines && !uncertain"
							variant="subtle"
							label="Start another record"
							:disabled="saving"
							@click="startAnotherRecord"
						/>
					</div>
					<p v-if="uncertain" class="mt-3 text-sm text-ink-orange-4" role="status">
						The save result is not confirmed. Retry uses the same record key and exact
						values. Do not edit this entry until it is confirmed.
					</p>
				</section>
			</div>
		</main>
	</div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { Badge, Breadcrumbs, Button } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { getCountPlan, recordCounts } from "@/api/countPlans";
import { errMessage } from "@/lib/errors";
import { siteTimezone, toSiteDatetime } from "@/utils/datetime";
import { session } from "@/data/session";

const props = defineProps({ request: { type: String, required: true } });
const plan = ref(null);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const message = ref("");
const quantities = reactive({});
const countedAt = ref(localDateTime());
const nowLocal = ref(localDateTime());
const entryKey = ref(newEntryKey());
const uncertain = ref(false);
let uncertainPayload = null;
let draftAllowed = true;
let loadSequence = 0;
// A browser can sign out and back in as another person without clearing
// sessionStorage. Keep one person's unfinished quantities out of the next
// person's view, even when they open the same approved request.
const storageKey = computed(
	() => "jarvis-count-plan:" + (session.user || "anonymous") + ":" + props.request
);

const recordPermission = computed(() => Number(plan.value?.can_record) === 1);
const remainingLines = computed(() =>
	(plan.value?.lines || []).filter((line) => !line.count_name)
);
const canRecord = computed(() => recordPermission.value && remainingLines.value.length > 0);
const hasRecordedLines = computed(() => plan.value?.lines?.some((line) => line.count_name));
const siteTimeZone = siteTimezone();
const entries = computed(() =>
	(plan.value?.lines || [])
		.filter((line) => !line.count_name && String(quantities[line.item_code] ?? "").trim())
		.map((line) => ({
			item_code: line.item_code,
			counted_qty: String(quantities[line.item_code]).trim(),
		}))
);

function localDateTime(date = new Date()) {
	const offset = date.getTimezoneOffset() * 60000;
	return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function newEntryKey() {
	const bytes = new Uint8Array(24);
	if (globalThis.crypto?.getRandomValues) {
		globalThis.crypto.getRandomValues(bytes);
		return "ccp_" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
	}
	return (
		"ccp_" +
		Date.now().toString(36) +
		"_" +
		Math.random().toString(36).slice(2).padEnd(20, "0")
	);
}

function readDraft() {
	draftAllowed = true;
	try {
		const saved = JSON.parse(sessionStorage.getItem(storageKey.value) || "null");
		if (!saved || typeof saved !== "object" || typeof saved.entryKey !== "string") return;
		entryKey.value = saved.entryKey;
		if (typeof saved.countedAt === "string") countedAt.value = saved.countedAt;
		if (saved.quantities && typeof saved.quantities === "object") {
			for (const [itemCode, value] of Object.entries(saved.quantities)) {
				if (typeof value === "string") quantities[itemCode] = value;
			}
		}
	} catch {
		// A malformed local draft must not block the approved plan.
	}
}

function writeDraft() {
	if (!draftAllowed) return;
	try {
		sessionStorage.setItem(
			storageKey.value,
			JSON.stringify({
				entryKey: entryKey.value,
				countedAt: countedAt.value,
				quantities: { ...quantities },
			})
		);
	} catch {
		// The server-side key still protects a retry when browser storage is unavailable.
	}
}

function clearDraft() {
	draftAllowed = false;
	try {
		sessionStorage.removeItem(storageKey.value);
	} catch {
		// No action is needed when storage is unavailable.
	}
}

function inputId(itemCode) {
	return "count-" + encodeURIComponent(itemCode);
}

function applyPlan(next) {
	plan.value = next;
	for (const line of next?.lines || []) {
		if (!(line.item_code in quantities)) quantities[line.item_code] = "";
	}
	if (
		next?.entry &&
		next.entry.entry_key === entryKey.value &&
		(uncertain.value || !message.value)
	) {
		uncertain.value = false;
		uncertainPayload = null;
		clearDraft();
		message.value = "This physical-count record was already saved.";
	}
}

async function load() {
	const sequence = ++loadSequence;
	loading.value = true;
	error.value = "";
	nowLocal.value = localDateTime();
	try {
		const next = await getCountPlan(props.request, entryKey.value);
		if (sequence !== loadSequence) return;
		applyPlan(next);
	} catch (cause) {
		if (sequence !== loadSequence) return;
		error.value = errMessage(cause, "Could not load this physical-count plan.");
	} finally {
		if (sequence === loadSequence) loading.value = false;
	}
}

function payload() {
	return {
		countedAt: toSiteDatetime(countedAt.value),
		entries: entries.value.map((entry) => ({ ...entry })),
	};
}

function equalPayload(left, right) {
	return JSON.stringify(left) === JSON.stringify(right);
}

function isConfirmedRefusal(cause) {
	return (
		[400, 403, 404, 417, 422].includes(Number(cause?.status)) ||
		["PermissionError", "ValidationError"].includes(cause?.exc_type)
	);
}

async function submit(forceRetry = false) {
	const next = payload();
	const request = props.request;
	if (!next.entries.length || !next.countedAt || saving.value) return;
	if (uncertain.value && (!forceRetry || !equalPayload(next, uncertainPayload))) {
		error.value =
			"Retry the exact saved values first. The prior save result is still not confirmed.";
		return;
	}
	saving.value = true;
	error.value = "";
	message.value = "";
	writeDraft();
	try {
		const result = await recordCounts(request, next.countedAt, next.entries, entryKey.value);
		if (request !== props.request) return;
		const saved = result?.saved?.[0];
		message.value = saved?.idempotent
			? "This physical-count record was already saved."
			: "Physical-count record saved.";
		uncertain.value = false;
		uncertainPayload = null;
		clearDraft();
		await load();
		entryKey.value = newEntryKey();
		draftAllowed = true;
		writeDraft();
	} catch (cause) {
		if (request !== props.request) return;
		const failure = errMessage(cause, "Could not confirm the physical-count save.");
		if (isConfirmedRefusal(cause)) {
			uncertain.value = false;
			uncertainPayload = null;
			error.value = failure;
		} else {
			uncertain.value = true;
			uncertainPayload = next;
			error.value = failure;
			await load();
			if (uncertain.value) error.value = failure;
		}
	} finally {
		saving.value = false;
	}
}

function save() {
	submit(false);
}
function retry() {
	submit(true);
}
function startAnotherRecord() {
	if (uncertain.value) return;
	draftAllowed = true;
	entryKey.value = newEntryKey();
	countedAt.value = localDateTime();
	for (const key of Object.keys(quantities)) quantities[key] = "";
	message.value = "Start a new record for the remaining planned items.";
	writeDraft();
}

function openRequest() {
	plan.value = null;
	saving.value = false;
	error.value = "";
	message.value = "";
	uncertain.value = false;
	uncertainPayload = null;
	entryKey.value = newEntryKey();
	countedAt.value = localDateTime();
	for (const key of Object.keys(quantities)) delete quantities[key];
	readDraft();
	load();
}

watch([quantities, countedAt], writeDraft, { deep: true });
watch(
	() => props.request,
	(request, previous) => {
		if (request && previous && request !== previous) openRequest();
	}
);
onMounted(() => {
	openRequest();
});
onBeforeUnmount(writeDraft);
</script>
