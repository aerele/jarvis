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
			<div class="mx-auto max-w-4xl">
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div>
						<h1 id="count-plan-title" class="text-xl font-semibold text-ink-gray-9">
							{{ plan?.title || plan?.warehouse || "Physical count plan" }}
						</h1>
						<p v-if="plan" class="mt-1 text-sm text-ink-gray-6">
							{{ plan.company }} · {{ plan.warehouse }}
						</p>
					</div>
					<Badge
						v-if="plan"
						variant="subtle"
						:theme="canSubmit ? 'green' : recordPermission ? 'blue' : 'gray'"
						:label="
							activeCorrection
								? 'Correcting observation'
								: canRecord
								? 'Ready to record'
								: canCorrect
								? 'Ready to correct'
								: recordPermission
								? 'All planned items recorded'
								: 'View only'
						"
					/>
				</div>
				<div v-if="error && !plan" class="py-10 text-sm text-ink-red-4" role="alert">
					{{ error }}
				</div>
				<div
					v-else-if="loading && !plan"
					class="flex items-center gap-2 py-10 text-sm text-ink-gray-5"
				>
					<JvSpinner /> Loading count plan…
				</div>
				<div v-else-if="plan">
					<p class="mt-4 text-sm text-ink-gray-6">
						Record only what you physically observed. Leave an item blank if you did
						not count it. A blank value is not zero. This page creates an immutable
						observation record; it does not adjust stock.
					</p>

					<p
						v-if="message"
						class="mt-4 rounded-lg bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
						role="status"
					>
						{{ message }}
					</p>
					<p v-if="error" class="mt-4 text-sm text-ink-red-4" role="alert">
						{{ error }}
					</p>

					<div
						v-if="!recordPermission"
						class="mt-4 rounded-lg border bg-surface-gray-1 px-4 py-3 text-sm text-ink-gray-6"
					>
						You can view this approved plan, but you do not have the current Stock User
						and read permissions that are required to record an observation.
					</div>
					<div
						v-else-if="!remainingLines.length && !canCorrect"
						class="mt-4 rounded-lg border bg-surface-gray-1 px-4 py-3 text-sm text-ink-gray-6"
					>
						Every planned item already has a submitted physical-count observation.
					</div>

					<section
						v-if="correctableCounts.length"
						class="mt-6 rounded-lg border p-4"
						aria-labelledby="correct-count-title"
					>
						<h2 id="correct-count-title" class="text-base font-medium text-ink-gray-9">
							Correct a recorded observation
						</h2>
						<p class="mt-1 text-sm text-ink-gray-6">
							A correction replaces one current observation with the same planned
							items. It does not adjust stock.
						</p>
						<div class="mt-3 grid gap-3">
							<div
								v-for="count in correctableCounts"
								:key="count.name"
								class="flex flex-wrap items-start justify-between gap-3 rounded-lg border p-3"
							>
								<div class="min-w-0 flex-1">
									<h3 class="text-sm font-medium text-ink-gray-9">
										Observation {{ count.name }}
									</h3>
									<div
										v-if="
											count.recorded_by ||
											count.counted_at ||
											count.recorded_at
										"
										class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-gray-6"
									>
										<span v-if="count.recorded_by"
											>Recorded by {{ count.recorded_by }}</span
										>
										<span v-if="count.counted_at"
											>Observed at {{ count.counted_at }}</span
										>
										<span v-if="count.recorded_at"
											>Recorded at {{ count.recorded_at }}</span
										>
									</div>
									<ul
										v-if="observationItems(count).length"
										class="mt-2 space-y-1 text-sm text-ink-gray-7"
									>
										<li
											v-for="item in observationItems(count)"
											:key="item.item_code"
										>
											{{ observationItemLabel(item) }}
										</li>
									</ul>
								</div>
								<Button
									variant="subtle"
									:label="
										activeCorrection && activeCorrection.name === count.name
											? 'Correcting ' + count.name
											: 'Correct ' + count.name
									"
									:disabled="saving || uncertain || !!activeCorrection"
									@click="startCorrection(count)"
								/>
							</div>
						</div>
						<div
							v-if="activeCorrection"
							class="mt-4 rounded-lg border bg-surface-gray-1 p-3"
						>
							<h3 class="text-sm font-medium text-ink-gray-9">
								Current observation {{ activeCorrection.name }}
							</h3>
							<dl
								v-if="
									activeCorrection.recorded_by ||
									activeCorrection.counted_at ||
									activeCorrection.recorded_at
								"
								class="mt-3 grid gap-x-4 gap-y-2 text-sm sm:grid-cols-3"
							>
								<div v-if="activeCorrection.recorded_by">
									<dt class="text-xs text-ink-gray-5">Recorded by</dt>
									<dd class="break-words text-ink-gray-7">
										{{ activeCorrection.recorded_by }}
									</dd>
								</div>
								<div v-if="activeCorrection.counted_at">
									<dt class="text-xs text-ink-gray-5">Observed at</dt>
									<dd class="break-words text-ink-gray-7">
										{{ activeCorrection.counted_at }}
									</dd>
								</div>
								<div v-if="activeCorrection.recorded_at">
									<dt class="text-xs text-ink-gray-5">Recorded at</dt>
									<dd class="break-words text-ink-gray-7">
										{{ activeCorrection.recorded_at }}
									</dd>
								</div>
							</dl>
							<div v-if="correctionItems.length" class="mt-3">
								<p class="text-xs text-ink-gray-5">Recorded items</p>
								<ul class="mt-1 space-y-1 text-sm text-ink-gray-7">
									<li v-for="item in correctionItems" :key="item.item_code">
										{{ observationItemLabel(item) }}
									</li>
								</ul>
							</div>
							<p class="mt-3 text-sm text-ink-gray-6">
								Saving cancels this current observation and creates a replacement
								observation. It does not adjust stock.
							</p>
							<div class="mt-3 flex flex-wrap items-center gap-2">
								<p class="text-sm text-ink-gray-6">
									Enter every corrected quantity for
									{{ activeCorrection.name }} below.
								</p>
								<Button
									variant="subtle"
									label="Cancel correction"
									:disabled="saving || uncertain"
									@click="cancelCorrection"
								/>
							</div>
						</div>
					</section>

					<section
						class="mt-6 overflow-hidden rounded-lg border"
						aria-labelledby="count-lines-title"
					>
						<div class="border-b bg-surface-gray-1 px-4 py-3">
							<h2
								id="count-lines-title"
								class="text-base font-medium text-ink-gray-9"
							>
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
									<div
										v-if="line.count_name"
										class="mt-1 text-xs text-ink-gray-6"
									>
										Recorded in {{ line.count_name }}
									</div>
								</div>
								<div class="text-sm text-ink-gray-6">
									<div class="text-xs text-ink-gray-5">
										Current book quantity
									</div>
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
										{{ quantityLabel(line) }}
									</label>
									<input
										:id="inputId(line.item_code)"
										v-model="quantities[line.item_code]"
										type="text"
										inputmode="decimal"
										autocomplete="off"
										placeholder="Leave blank"
										:aria-label="quantityLabel(line)"
										:disabled="!canEditLine(line) || saving || uncertain"
										class="w-full rounded border border-outline-gray-2 px-2 py-1.5 text-sm disabled:bg-surface-gray-1"
									/>
								</div>
							</div>
						</div>
					</section>

					<section
						v-if="canSubmit"
						class="mt-6 rounded-lg border p-4"
						aria-labelledby="record-count-title"
					>
						<h2 id="record-count-title" class="text-base font-medium text-ink-gray-9">
							{{
								activeCorrection
									? "Save corrected observation"
									: "Save observation"
							}}
						</h2>
						<p v-if="activeCorrection" class="mt-2 text-sm text-ink-gray-6">
							Saving cancels the current observation and creates a replacement
							observation. It does not adjust stock.
						</p>
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
								:label="
									activeCorrection
										? 'Save corrected count'
										: 'Save physical count'
								"
								:loading="saving"
								:disabled="!canSave || !countedAt || uncertain"
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
								v-if="hasRecordedLines && !uncertain && !activeCorrection"
								variant="subtle"
								label="Start another record"
								:disabled="saving"
								@click="startAnotherRecord"
							/>
						</div>
						<p v-if="uncertain" class="mt-3 text-sm text-ink-orange-4" role="status">
							The save result is not confirmed. Retry uses the same record key and
							exact values. Do not edit this entry until it is confirmed.
						</p>
					</section>
				</div>
			</div>
		</main>
	</div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { Badge, Breadcrumbs, Button } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { correctCounts, getCountPlan, recordCounts } from "@/api/countPlans";
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
const correctionName = ref("");
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
const activeCounts = computed(() =>
	Array.isArray(plan.value?.active_counts) ? plan.value.active_counts : []
);
const correctableCounts = computed(() =>
	activeCounts.value.filter((count) => Number(count?.can_correct) === 1)
);
const activeCorrection = computed(
	() => correctableCounts.value.find((count) => count.name === correctionName.value) || null
);
const correctionItems = computed(() => observationItems(activeCorrection.value));
const canCorrect = computed(() => correctableCounts.value.length > 0);
const canSubmit = computed(() => canRecord.value || !!activeCorrection.value || uncertain.value);
const editableLines = computed(() => {
	if (!activeCorrection.value) return remainingLines.value;
	const correctedItems = new Set(
		(activeCorrection.value.items || []).map((item) => item.item_code)
	);
	return (plan.value?.lines || []).filter((line) => correctedItems.has(line.item_code));
});
const siteTimeZone = siteTimezone();
const entries = computed(() =>
	editableLines.value
		.filter((line) => String(quantities[line.item_code] ?? "").trim())
		.map((line) => ({
			item_code: line.item_code,
			counted_qty: String(quantities[line.item_code]).trim(),
		}))
);
const canSave = computed(
	() =>
		entries.value.length > 0 &&
		(!activeCorrection.value || entries.value.length === editableLines.value.length)
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
		if (typeof saved.replaces === "string") correctionName.value = saved.replaces;
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
				replaces: correctionName.value,
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

function observationItems(count) {
	return Array.isArray(count?.items) ? count.items : [];
}

function quantityLabel(line) {
	const itemCode =
		typeof line?.item_code === "string" && line.item_code ? line.item_code : "item";
	const stockUom =
		typeof line?.stock_uom === "string" && line.stock_uom
			? line.stock_uom
			: "stock unit unavailable";
	return "Physical count for " + itemCode + " (" + stockUom + ")";
}

function observationItemLabel(item) {
	const itemCode =
		typeof item?.item_code === "string" && item.item_code ? item.item_code : "Item";
	const quantity = item?.counted_qty == null ? "Not recorded" : String(item.counted_qty);
	const stockUom =
		typeof item?.stock_uom === "string" && item.stock_uom ? " " + item.stock_uom : "";
	return itemCode + ": " + quantity + stockUom;
}

function canEditLine(line) {
	if (!canSubmit.value) return false;
	if (!activeCorrection.value) return !line.count_name;
	return (activeCorrection.value.items || []).some((item) => item.item_code === line.item_code);
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
		correctionName.value = "";
		clearDraft();
		message.value = "This physical-count record was already saved.";
	} else if (correctionName.value && !activeCorrection.value && !uncertain.value) {
		correctionName.value = "";
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
		replaces: correctionName.value,
	};
}

function isConfirmedRefusal(cause) {
	return (
		[400, 403, 404, 417, 422].includes(Number(cause?.status)) ||
		["PermissionError", "ValidationError"].includes(cause?.exc_type)
	);
}

async function submit(forceRetry = false) {
	const next = uncertain.value ? uncertainPayload : payload();
	const request = props.request;
	if (!next.entries.length || !next.countedAt || saving.value) return;
	if (uncertain.value && !forceRetry) {
		error.value =
			"Retry the exact saved values first. The prior save result is still not confirmed.";
		return;
	}
	saving.value = true;
	error.value = "";
	message.value = "";
	writeDraft();
	try {
		const result = next.replaces
			? await correctCounts(
					request,
					next.replaces,
					next.countedAt,
					next.entries,
					entryKey.value
			  )
			: await recordCounts(request, next.countedAt, next.entries, entryKey.value);
		if (request !== props.request) return;
		const saved = result?.saved?.[0];
		message.value = saved?.idempotent
			? next.replaces
				? "This corrected physical-count record was already saved."
				: "This physical-count record was already saved."
			: next.replaces
			? "Corrected physical-count record saved."
			: "Physical-count record saved.";
		uncertain.value = false;
		uncertainPayload = null;
		clearDraft();
		await load();
		entryKey.value = newEntryKey();
		correctionName.value = "";
		draftAllowed = true;
		writeDraft();
	} catch (cause) {
		if (request !== props.request) return;
		const failure = errMessage(
			cause,
			next.replaces
				? "Could not confirm the corrected physical-count save."
				: "Could not confirm the physical-count save."
		);
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
function startCorrection(count) {
	if (uncertain.value || activeCorrection.value || Number(count?.can_correct) !== 1) return;
	correctionName.value = count.name;
	entryKey.value = newEntryKey();
	countedAt.value = localDateTime();
	for (const item of count.items || []) quantities[item.item_code] = String(item.counted_qty);
	message.value = "Correct the recorded quantities, then save the replacement observation.";
	draftAllowed = true;
	writeDraft();
}
function cancelCorrection() {
	if (uncertain.value) return;
	for (const item of activeCorrection.value?.items || []) quantities[item.item_code] = "";
	correctionName.value = "";
	entryKey.value = newEntryKey();
	countedAt.value = localDateTime();
	message.value = "Correction cancelled. No observation was changed.";
	draftAllowed = true;
	writeDraft();
}
function startAnotherRecord() {
	if (uncertain.value) return;
	draftAllowed = true;
	correctionName.value = "";
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
	correctionName.value = "";
	entryKey.value = newEntryKey();
	countedAt.value = localDateTime();
	for (const key of Object.keys(quantities)) delete quantities[key];
	readDraft();
	load();
}

watch([quantities, countedAt, correctionName], writeDraft, { deep: true });
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
