<template>
	<div class="flex h-full min-h-0 flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: 'Physical count plans' }]" />
			</template>
			<template #right-header>
				<Button icon="refresh-cw" label="Refresh" :loading="loading" @click="load" />
			</template>
		</LayoutHeader>

		<main class="min-h-0 flex-1 overflow-y-auto px-5 py-6" aria-labelledby="count-plans-title">
			<div class="max-w-5xl">
				<h1 id="count-plans-title" class="text-xl font-semibold text-ink-gray-9">
					Physical count plans
				</h1>
				<p class="mt-1 text-sm text-ink-gray-6">
					Approved plans that you can view. Recording a count creates a human
					observation. It does not post stock or reconcile inventory.
				</p>

				<div
					v-if="refreshError && !rows.length"
					class="mt-6 flex flex-wrap items-center gap-3 text-sm text-ink-red-4"
					role="alert"
				>
					<span>{{ refreshError }}</span>
					<Button
						label="Retry refresh"
						variant="subtle"
						:loading="loading"
						@click="load"
					/>
				</div>
				<div
					v-else-if="loading && !rows.length"
					class="mt-10 flex items-center gap-2 text-sm text-ink-gray-5"
				>
					<JvSpinner /> Loading count plans…
				</div>
				<div
					v-else-if="!rows.length"
					class="mt-10 rounded-lg border bg-surface-gray-1 px-5 py-8 text-sm text-ink-gray-6"
				>
					{{
						hasMore
							? "Some approved plans could not be read. Load more to continue."
							: "No approved physical-count plans are ready for you."
					}}
				</div>
				<div v-else>
					<div
						v-if="refreshError"
						class="mt-6 flex flex-wrap items-center gap-3 text-sm text-ink-red-4"
						role="alert"
					>
						<span>{{ refreshError }}</span>
						<Button
							label="Retry refresh"
							variant="subtle"
							:loading="loading"
							@click="load"
						/>
					</div>
					<div class="mt-6 overflow-hidden rounded-lg border">
						<button
							v-for="row in rows"
							:key="row.request"
							class="flex w-full items-center gap-4 border-b px-4 py-4 text-left last:border-b-0 hover:bg-surface-gray-1 focus-visible:outline focus-visible:outline-2"
							@click="open(row.request)"
						>
							<div class="min-w-0 flex-1">
								<div class="truncate text-base font-medium text-ink-gray-9">
									{{ row.title || row.warehouse }}
								</div>
								<div class="mt-1 truncate text-sm text-ink-gray-6">
									{{ row.company }} · {{ row.warehouse }} ·
									{{ row.line_count }} item{{
										Number(row.line_count) === 1 ? "" : "s"
									}}
								</div>
							</div>
							<Badge
								variant="subtle"
								:theme="planStatus(row).theme"
								:label="planStatus(row).label"
							/>
							<FeatherIcon
								name="chevron-right"
								class="size-4 shrink-0 text-ink-gray-5"
							/>
						</button>
					</div>
				</div>
				<div v-if="hasMore || loadMoreError" class="mt-4">
					<Button
						:label="loadMoreError ? 'Retry load more' : 'Load more'"
						variant="subtle"
						:loading="loadingMore"
						@click="loadMore"
					/>
					<p v-if="loadMoreError" class="mt-2 text-sm text-ink-red-4" role="alert">
						{{ loadMoreError }}
					</p>
				</div>
			</div>
		</main>
	</div>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { Badge, Breadcrumbs, Button, FeatherIcon } from "frappe-ui";
import { useRouter } from "vue-router";
import LayoutHeader from "@/components/LayoutHeader.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { listCountPlans } from "@/api/countPlans";
import { errMessage } from "@/lib/errors";

const router = useRouter();
const rows = ref([]);
const start = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const loadingMore = ref(false);
const refreshError = ref("");
const loadMoreError = ref("");

async function fetchPage(offset, append = false) {
	const response = await listCountPlans({ start: offset, pageLength: 20 });
	const page = Array.isArray(response?.rows) ? response.rows : [];
	rows.value = append ? [...rows.value, ...page] : page;
	const nextStart = Number(response?.next_start);
	start.value =
		Number.isInteger(nextStart) && nextStart >= offset
			? nextStart
			: Number(response?.start || offset) + page.length;
	hasMore.value = Boolean(response?.has_more);
}

async function load() {
	if (loading.value || loadingMore.value) return;
	loading.value = true;
	refreshError.value = "";
	loadMoreError.value = "";
	try {
		await fetchPage(0);
	} catch (cause) {
		refreshError.value = errMessage(cause, "Could not load physical-count plans.");
	} finally {
		loading.value = false;
	}
}

async function loadMore() {
	if (loadingMore.value || loading.value) return;
	loadingMore.value = true;
	loadMoreError.value = "";
	try {
		await fetchPage(start.value, true);
	} catch (cause) {
		loadMoreError.value = errMessage(cause, "Could not load more physical-count plans.");
	} finally {
		loadingMore.value = false;
	}
}

function count(value) {
	if (
		(typeof value !== "number" && typeof value !== "string") ||
		(typeof value === "string" && !value.trim())
	) {
		return null;
	}
	const numeric = Number(value);
	return Number.isInteger(numeric) && numeric >= 0 ? numeric : null;
}

function planStatus(row) {
	const lineCount = count(row.line_count);
	const recordedLineCount = count(row.recorded_line_count);
	const remainingLineCount = count(row.remaining_line_count);
	const allItemsRecorded =
		lineCount !== null &&
		lineCount > 0 &&
		((recordedLineCount !== null && recordedLineCount >= lineCount) ||
			remainingLineCount === 0);

	if (allItemsRecorded) return { theme: "blue", label: "All items recorded" };
	if (recordedLineCount !== null && recordedLineCount > 0) {
		return Number(row.can_record)
			? { theme: "green", label: "Continue recording" }
			: { theme: "gray", label: "Partly recorded" };
	}
	return Number(row.can_record)
		? { theme: "green", label: "Ready to record" }
		: { theme: "gray", label: "View only" };
}

function open(request) {
	router.push({ name: "CountPlanDetail", params: { request } });
}

onMounted(load);
</script>
