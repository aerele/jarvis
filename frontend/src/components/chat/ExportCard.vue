<script setup>
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
			<span v-if="state?.status === 'running'" class="jv-export-pulse" aria-hidden="true" />
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
			<p>{{ state.filename }} · {{ (state.size_bytes / 1024 / 1024).toFixed(2) }} MB</p>
			<p>Made at {{ date(state.finished_at) }} from the data at that time.</p>
			<p>Available until {{ date(state.expires_at) }}</p>
			<a :href="download" download class="jv-export-button"
				>Download {{ state.format?.toUpperCase() }}</a
			>
		</template>
		<button
			v-if="active"
			class="jv-export-button"
			:disabled="cancelling || state.cancel_requested"
			@click="cancel"
		>
			{{
				cancelling || state.cancel_requested ? "Cancellation requested…" : "Cancel export"
			}}
		</button>
		<p v-if="active">Cancellation can take about five seconds.</p>
		<p v-if="entry?.error || cancelError" role="alert">
			{{ cancelError || entry.error }} <button @click="refreshExports">Retry</button>
		</p>
	</section>
</template>

<style scoped>
.jv-export-card {
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--surface-1, var(--surface));
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
.jv-export-button {
	display: inline-flex;
	justify-content: center;
	padding: 9px 14px;
	margin-top: 8px;
	border: 1px solid var(--border);
	border-radius: 8px;
	font: inherit;
	color: inherit;
	background: var(--surface-2, transparent);
	cursor: pointer;
	text-decoration: none;
}
.jv-export-button:disabled {
	opacity: 0.6;
	cursor: default;
}
.jv-export-pulse {
	width: 12px;
	height: 12px;
	border: 2px solid var(--border);
	border-top-color: currentColor;
	border-radius: 50%;
	animation: export-spin 1s linear infinite;
}
@keyframes export-spin {
	to {
		transform: rotate(360deg);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-export-pulse {
		animation: none;
	}
}
@media (max-width: 480px) {
	.jv-export-button {
		display: flex;
		box-sizing: border-box;
		width: 100%;
	}
}
</style>
