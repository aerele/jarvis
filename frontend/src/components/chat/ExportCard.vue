<script setup>
import { Button, LoadingIndicator } from "frappe-ui";
import { computed, onUnmounted, ref, watch } from "vue";
import { cancelExport, exportJobs, refreshExports, watchExport } from "../../lib/exportJobs.js";

const props = defineProps({ summary: { type: Object, required: true } });
let unsubscribe;
const cancelling = ref(false);
const cancelError = ref("");
watch(
	() => props.summary.export_job,
	(id) => {
		unsubscribe?.();
		unsubscribe = watchExport(id);
	},
	{ immediate: true }
);
onUnmounted(() => unsubscribe?.());
const entry = computed(() => exportJobs[props.summary.export_job]);
const state = computed(() => entry.value?.state);
const missing = computed(() => entry.value && entry.value.state === null);
const active = computed(() => ["queued", "running", "paused"].includes(state.value?.status));
const labels = {
	queued: "Queued behind another export",
	running: "Preparing your export",
	paused: "Paused for a site update",
	ready: "Your export is ready",
	"too large": "Export too large",
	"too slow": "Export took too long",
	failed: "Export failed",
	cancelled: "Export cancelled",
	expired: "This file has expired; ask again",
};
const label = computed(() =>
	missing.value
		? "This export is no longer available"
		: labels[state.value?.status] || "Checking export status…"
);
const download = computed(() => {
	const url = state.value?.file_url;
	return state.value?.status === "ready" &&
		typeof url === "string" &&
		url.startsWith("/private/files/")
		? url
		: null;
});
const date = (value) => (value ? new Date(String(value).replace(" ", "T")).toLocaleString() : "");
const elapsed = computed(() => {
	if (!state.value?.started_at) return "";
	const end = state.value.finished_at
		? new Date(String(state.value.finished_at).replace(" ", "T")).getTime()
		: Date.now();
	const seconds = Math.max(
		0,
		Math.round(
			(end - new Date(String(state.value.started_at).replace(" ", "T")).getTime()) / 1000
		)
	);
	return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
});
function downloadFile() {
	if (!download.value) return;
	const link = document.createElement("a");
	link.href = download.value;
	link.download = state.value.filename || "";
	document.body.appendChild(link);
	link.click();
	link.remove();
}
async function cancel() {
	cancelling.value = true;
	cancelError.value = "";
	try {
		await cancelExport(props.summary.export_job);
	} catch (error) {
		cancelError.value = error.message;
	} finally {
		cancelling.value = false;
	}
}
</script>

<template>
	<section class="jv-export-card" aria-label="File export">
		<div class="jv-export-heading">
			<div>
				<strong>{{ state?.title || summary.title || "Export" }}</strong>
				<p role="status">{{ label }}</p>
			</div>
			<LoadingIndicator
				v-if="state?.status === 'running'"
				class="h-4 w-4"
				aria-hidden="true"
			/>
		</div>
		<p v-if="state">
			{{ Number(state.rows_written || 0).toLocaleString() }} rows<span v-if="elapsed">
				· {{ elapsed }}</span
			>
		</p>
		<template v-if="missing">
			<p>{{ Number(summary.rows_written || 0).toLocaleString() }} rows</p>
			<p v-if="summary.finished_at">Made at {{ date(summary.finished_at) }}.</p>
		</template>
		<p v-if="state?.status === 'paused' && state.next_attempt">
			Retry planned for {{ date(state.next_attempt) }}
		</p>
		<p v-if="state?.error">{{ state.error }}</p>
		<p v-if="state?.limited_by_spec">
			Contains only the first {{ state.limited_by_spec }} rows requested.
		</p>
		<p v-if="state?.cells_truncated">
			Some long cells were shortened; the row count is complete.
		</p>
		<template v-if="download">
			<p class="jv-file-chip">
				{{ state.filename }} · {{ (state.size_bytes / 1024 / 1024).toFixed(2) }} MB
			</p>
			<p>Made at {{ date(state.finished_at) }} from the data at that time.</p>
			<p>Available until {{ date(state.expires_at) }}</p>
			<Button variant="subtle" data-testid="download-export" @click="downloadFile">
				Download {{ state.format?.toUpperCase() }}
			</Button>
		</template>
		<Button
			v-if="active"
			variant="subtle"
			:loading="cancelling"
			:disabled="cancelling || state.cancel_requested"
			@click="cancel"
		>
			{{
				cancelling || state.cancel_requested ? "Cancellation requested…" : "Cancel export"
			}}
		</Button>
		<p v-if="active">Cancellation can take about five seconds.</p>
		<p v-if="entry?.error || cancelError" role="alert">
			{{ cancelError || entry.error }}
			<Button variant="subtle" @click="refreshExports()">Retry</Button>
		</p>
	</section>
</template>

<style scoped>
.jv-export-card {
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--surface-1, var(--card));
	color: var(--text, var(--ink9));
	padding: 16px;
	margin: 12px 0;
	max-width: 560px;
	overflow-wrap: anywhere;
	font-size: 13px;
}
.jv-export-card p {
	margin: 6px 0;
	color: var(--text-2, var(--ink7));
	line-height: 1.5;
}
.jv-export-heading {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
}
.jv-file-chip {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	max-width: 100%;
	padding: 6px 11px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-2, var(--card2));
	font-size: 12.5px;
	line-height: 1.3;
	overflow-wrap: anywhere;
}
</style>
