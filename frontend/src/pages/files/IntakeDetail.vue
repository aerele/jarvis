<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="breadcrumbs" />
			</template>
		</LayoutHeader>

		<div v-if="error" class="flex flex-1 flex-col items-center justify-center gap-1 px-5">
			<div class="text-lg font-medium text-ink-gray-8">File not found</div>
			<div class="text-center text-p-base text-ink-gray-6">{{ error }}</div>
			<Button class="mt-3" label="Back to File Box" @click="router.push(backRoute)" />
		</div>

		<div v-else-if="data" class="flex-1 overflow-y-auto">
			<div class="mx-auto w-full max-w-6xl px-5 py-6">
				<div
					class="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]"
					data-testid="intake-detail"
				>
					<IntakeSourcePreview :file="previewFile" tall />

					<div class="flex min-w-0 flex-col gap-6">
						<!-- Title, then the state of the file and its one primary action -->
						<section aria-label="Status" class="flex flex-col gap-3">
							<div class="flex min-w-0 items-center gap-2">
								<h1 class="min-w-0 truncate text-xl font-semibold text-ink-gray-9">
									{{ data.row.file_name || data.row.name }}
								</h1>
								<Badge
									variant="subtle"
									:theme="badge.theme"
									:class="badgeClass"
									:label="badge.label"
								/>
							</div>
							<div class="flex flex-col gap-1" data-testid="intake-state">
								<h2
									v-if="state.heading"
									class="text-sm font-medium text-ink-gray-8"
								>
									{{ state.heading }}
								</h2>
								<p class="break-words text-base text-ink-gray-8">
									{{ state.line }}
								</p>
							</div>
							<div class="flex flex-wrap items-center gap-2">
								<Button
									v-if="data.can_retry"
									:variant="primary === 'retry' ? 'solid' : 'subtle'"
									label="Retry"
									:loading="retrying"
									@click="retry"
								/>
								<Button
									v-if="link && link.kind === 'route' && data.can_review"
									:variant="primary === 'review' ? 'solid' : 'subtle'"
									label="Open review"
									@click="router.push(link.href)"
								/>
								<Button
									v-if="link && link.kind === 'desk'"
									:variant="primary === 'draft' ? 'solid' : 'subtle'"
									label="Open draft"
									@click="openDraft"
								/>
								<Button
									v-if="data.file"
									variant="ghost"
									label="Open file"
									icon-left="external-link"
									@click="openFile"
								/>
							</div>
						</section>

						<section v-if="rows.length || table" aria-label="What was read">
							<h2 class="text-sm font-medium text-ink-gray-8">What was read</h2>
							<dl
								v-if="rows.length"
								class="mt-2 grid grid-cols-[minmax(6rem,35%)_1fr] gap-x-4 gap-y-3"
							>
								<template v-for="r in rows" :key="r.fieldname">
									<dt class="text-sm text-ink-gray-6">{{ r.label }}</dt>
									<dd
										class="min-w-0 break-words text-base text-ink-gray-8"
										:data-field="r.fieldname"
									>
										<template v-if="r.filled">
											<span>{{ r.draft }}</span>
											<p
												class="mt-0.5 text-sm text-ink-gray-6"
												data-testid="intake-filled"
											>
												Filled in by the reviewer
											</p>
										</template>
										<span v-else-if="r.read">{{ r.read }}</span>
										<span v-else class="text-ink-gray-5"
											>Not found on the page</span
										>
										<p
											v-if="r.changed"
											class="mt-0.5 text-sm text-ink-gray-6"
											data-testid="intake-change"
										>
											On the draft: {{ r.draft || "blank"
											}}<template v-if="r.why">. {{ r.why }}</template>
										</p>
									</dd>
								</template>
							</dl>
							<ol
								v-if="table && data.reading.table_rows.length"
								class="mt-4 flex flex-col gap-2"
								:aria-label="table.label"
							>
								<li
									v-for="(cells, i) in data.reading.table_rows"
									:key="i"
									data-testid="intake-row"
								>
									<dl
										class="grid grid-cols-[minmax(5rem,40%)_1fr] gap-x-4 gap-y-0.5 rounded bg-surface-gray-1 p-2"
									>
										<dt class="col-span-2 text-sm font-medium text-ink-gray-8">
											{{ table.label }} {{ i + 1 }}
										</dt>
										<template
											v-for="(c, j) in table.columns"
											:key="c.fieldname"
										>
											<dt class="text-sm text-ink-gray-6">{{ c.label }}</dt>
											<dd
												class="min-w-0 break-words text-base text-ink-gray-8"
											>
												{{ cells[j] }}
											</dd>
										</template>
									</dl>
								</li>
							</ol>
						</section>

						<section aria-label="Details">
							<h2 class="text-sm font-medium text-ink-gray-8">Details</h2>
							<dl class="mt-2 grid grid-cols-[minmax(6rem,35%)_1fr] gap-x-4 gap-y-1">
								<template v-for="d in details" :key="d.label">
									<dt class="text-sm text-ink-gray-6">{{ d.label }}</dt>
									<dd class="min-w-0 break-words text-sm text-ink-gray-7">
										{{ d.value }}
									</dd>
								</template>
							</dl>
						</section>
					</div>
				</div>
			</div>
		</div>
		<div v-else class="flex-1" />
	</div>
</template>

<script setup>
// File Box > Intake > one row: the source file beside what was read from it and, when
// the caller may see the draft, what the draft holds. Read-only; Retry, Open draft and
// Open review are the only actions. Polls every 10 s while the file is Queued or Running.
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Badge, Breadcrumbs, Button, toast } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import IntakeSourcePreview from "@/components/IntakeSourcePreview.vue";
import { intakeDetail, intakeRetry } from "@/api/intake";
import { errHtml, errMessage } from "@/lib/errors";
import { formatDate } from "@/utils/datetime";
import { intakeBadge, intakeBadgeClass, intakeDraftLink, intakeIsLive } from "@/lib/intakeStatus";

const route = useRoute();
const router = useRouter();
const backRoute = { name: "FilesList", query: { view: "intake" } };

const data = ref(null);
const error = ref("");
const retrying = ref(false);

const breadcrumbs = computed(() => [
	{ label: "File Box", route: backRoute },
	{ label: data.value?.row.file_name || "Intake file" },
]);

// The list's row shape plus the server's `badge` hint, so the badge matches the list.
const listRow = computed(() =>
	data.value
		? { ...data.value.row, badge: data.value.badge, can_review: data.value.can_review }
		: null
);
const badge = computed(() => intakeBadge(listRow.value));
const badgeClass = computed(() => intakeBadgeClass(listRow.value));
const link = computed(() => intakeDraftLink(listRow.value));
const previewFile = computed(() => {
	const f = data.value?.file;
	return f
		? {
				file_url: f.url,
				file_name: data.value.row.file_name,
				is_image: f.is_image,
				is_pdf: f.is_pdf,
		  }
		: null;
});

const spec = computed(() => data.value?.spec || { fields: [], table: null });
const table = computed(() => spec.value.table);
const rows = computed(() => {
	const d = data.value;
	if (!d) return [];
	return spec.value.fields.map((f) => ({
		fieldname: f.fieldname,
		label: f.label,
		read: d.reading.values[f.fieldname] || "",
		draft: d.draft ? d.draft.values[f.fieldname] || "" : "",
		filled: !!d.draft && (d.reviewer_filled || []).includes(f.fieldname),
		changed:
			!!d.draft &&
			f.fieldname in d.changes &&
			!(d.reviewer_filled || []).includes(f.fieldname),
		why: d.changes[f.fieldname] || "",
	}));
});

const labelOf = (fieldname) =>
	spec.value.fields.find((f) => f.fieldname === fieldname)?.label || fieldname;

// What the page says about the file's state, in plain words (the server keeps the summary).
const state = computed(() => {
	const d = data.value;
	const status = d.row.status;
	const missing = d.missing.map(labelOf);
	if (d.row.draft_missing) return { line: "The draft was deleted." };
	if (status === "Queued") {
		return { line: d.badge === "waiting" ? d.row.summary : "Waiting to be read." };
	}
	if (status === "Running") return { line: "Reading now." };
	if (status === "Failed" || status === "Skipped") {
		return {
			heading: status === "Failed" ? "Why it failed" : "Why it was skipped",
			line: d.row.summary,
		};
	}
	if (status === "Needs Review") {
		return {
			line: missing.length ? `Not found on the page: ${missing.join(", ")}` : d.row.summary,
		};
	}
	if (d.row.draft_name) {
		return { line: `Draft created: ${d.row.draft_doctype} ${d.row.draft_name}` };
	}
	return { line: d.row.summary };
});

const primary = computed(() => {
	const d = data.value;
	if (d.can_retry) return "retry";
	if (d.row.status === "Needs Review" && d.can_review) return "review";
	if (link.value && link.value.kind === "desk") return "draft";
	return "";
});

const details = computed(() => {
	const r = data.value.row;
	return [
		{ label: "File", value: r.file_name },
		{ label: "Source", value: r.source_label },
		{ label: "Added", value: formatDate(r.creation, "MMM D, YYYY h:mm A") },
	].filter((d) => d.value);
});

function openDraft() {
	window.open(link.value.href, "_blank", "noopener,noreferrer");
}
function openFile() {
	window.open(data.value.file.url, "_blank", "noopener,noreferrer");
}

async function load() {
	try {
		data.value = await intakeDetail(route.params.activity);
		error.value = "";
	} catch (e) {
		// A refresh that fails keeps the page that is already on screen.
		if (!data.value) error.value = errMessage(e, "This file could not be opened.");
	}
}
async function retry() {
	if (retrying.value) return;
	retrying.value = true;
	try {
		await intakeRetry(data.value.row.name);
		toast.success("Queued again");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		retrying.value = false;
		load();
	}
}

let timer = null;
onMounted(async () => {
	await load();
	timer = setInterval(() => {
		if (!document.hidden && data.value && intakeIsLive(data.value.row)) load();
	}, 10000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>
