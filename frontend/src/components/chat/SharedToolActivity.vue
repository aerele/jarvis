<template>
	<div v-if="!decision" class="jv-tool">
		<button type="button" class="jv-tool-head" :aria-expanded="state.open" @click="toggle">
			<span class="jv-tool-dot" :class="tone" />
			<span class="jv-tool-name">{{ message.tool_name || __("Tool activity") }}</span>
			<span class="jv-tool-status">{{ status }}</span>
			<svg
				class="jv-tool-chev"
				:class="{ open: state.open }"
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				aria-hidden="true"
			>
				<path d="m9 18 6-6-6-6" />
			</svg>
		</button>
		<div v-if="state.open" class="jv-tool-detail">
			<p v-if="state.error" role="status">{{ state.error }}</p>
			<p v-else-if="state.loading" role="status">{{ __("Loading tool details…") }}</p>
			<template v-else-if="state.detail">
				<h4 class="jv-tool-io-k">{{ __("Input") }}</h4>
				<pre class="jv-tool-io">{{ display(state.detail.tool_args) }}</pre>
				<h4 class="jv-tool-io-k">{{ __("Output") }}</h4>
				<pre class="jv-tool-io">{{ display(state.detail.tool_result) }}</pre>
			</template>
		</div>
	</div>
</template>
<script setup>
import { shallowRef, reactive, computed, watch, inject, onBeforeUnmount } from "vue";
import { getSharedToolDetail } from "@/api/chatSharing";
import { __ } from "@/lib/i18n";
const props = defineProps({
	message: { type: Object, required: true },
	loadDetail: { type: Function, default: getSharedToolDetail },
	startOpen: { type: Boolean, default: false },
});
const sharedCache = inject("sharedToolDetails", null),
	cache = sharedCache || new Map();
const state = shallowRef(null);
const decision = computed(
	() =>
		props.message.awaiting_actor ||
		props.message.awaiting_owner ||
		props.message.control ||
		props.message.action_outcome ||
		props.message.tool_status === "pending"
);
const tone = computed(() =>
	["error", "failed"].includes(props.message.tool_status)
		? "err"
		: ["running", "started"].includes(props.message.tool_status)
		? "run"
		: "ok"
);
const status = computed(() =>
	tone.value === "err"
		? __("Failed")
		: tone.value === "run"
		? __("Running")
		: props.message.tool_status === "completed"
		? __("Completed")
		: __("Tool activity")
);
function display(value) {
	let text =
		value == null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
	try {
		text = JSON.stringify(JSON.parse(text), null, 2);
	} catch {
		/* Plain text is already safe Vue text. */
	}
	return text.length > 4000 ? text.slice(0, 4000) + "\n… " + __("(truncated)") : text;
}
async function load(entry) {
	if (!entry.valid || decision.value || !entry.open || entry.detail || entry.loading) return;
	const version = entry.version,
		name = props.message.name,
		reader = props.loadDetail;
	entry.loading = true;
	entry.error = "";
	try {
		const value = await reader(name);
		if (entry.valid && version === entry.version) entry.detail = value;
	} catch {
		if (entry.valid && version === entry.version)
			entry.error = __(
				"Couldn't load these details. Close and expand this row to try again."
			);
	} finally {
		if (entry.valid && version === entry.version) entry.loading = false;
	}
}
watch(
	[
		() => props.message.name,
		() => props.message.modified,
		() => props.message.tool_status,
		() => props.loadDetail,
		() => !!decision.value,
	],
	([name, modified, status, reader, blocked]) => {
		let entry = cache.get(name);
		if (!entry) {
			entry = reactive({
				open: props.startOpen,
				loading: false,
				detail: null,
				error: "",
				valid: true,
				version: 0,
				modified,
				status,
				reader,
				blocked,
			});
			cache.set(name, entry);
		} else if (
			entry.modified !== modified ||
			entry.status !== status ||
			entry.reader !== reader ||
			entry.blocked !== blocked
		) {
			entry.version++;
			entry.detail = null;
			entry.loading = false;
			entry.error = "";
			entry.modified = modified;
			entry.status = status;
			entry.reader = reader;
			entry.blocked = blocked;
		}
		state.value = entry;
		if (blocked) {
			entry.open = false;
			entry.detail = null;
		} else load(entry);
	},
	{ immediate: true }
);
function toggle() {
	state.value.open = !state.value.open;
	load(state.value);
}
onBeforeUnmount(() => {
	if (!sharedCache) {
		for (const entry of cache.values()) {
			entry.valid = false;
			entry.version++;
		}
	}
});
</script>
<style scoped>
.jv-tool {
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface);
	overflow: hidden;
	min-width: 0;
}
.jv-tool-head {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	padding: 7px 10px;
	background: transparent;
	border: none;
	cursor: pointer;
	font: inherit;
	font-size: 12.5px;
	color: var(--text);
	text-align: left;
}
.jv-tool-head:hover {
	background: var(--surface-1);
}
.jv-tool-dot {
	width: 7px;
	height: 7px;
	border-radius: 50%;
	flex: none;
}
.jv-tool-dot.ok {
	background: var(--green);
}
.jv-tool-dot.err {
	background: var(--red);
}
.jv-tool-dot.run {
	background: var(--amber);
}
.jv-tool-name {
	font-weight: 550;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 12px;
	overflow-wrap: anywhere;
	min-width: 0;
}
.jv-tool-status {
	margin-left: auto;
	font-size: 11px;
	color: var(--text-3);
	flex: none;
}
.jv-tool-chev {
	flex: none;
	color: var(--text-3);
	transition: transform 0.15s ease;
}
.jv-tool-chev.open {
	transform: rotate(90deg);
}
.jv-tool-detail {
	padding: 4px 11px 11px;
	border-top: 1px solid var(--border);
}
.jv-tool-detail p {
	font-size: 12px;
	overflow-wrap: anywhere;
}
.jv-tool-io-k {
	font-size: 10.5px;
	font-weight: 700;
	letter-spacing: 0.04em;
	text-transform: uppercase;
	color: var(--text-3);
	margin: 9px 0 4px;
}
.jv-tool-io {
	margin: 0;
	padding: 9px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 7px;
	font: 11.5px/1.5 ui-monospace, "SF Mono", Menlo, monospace;
	color: var(--text);
	white-space: pre-wrap;
	word-break: break-word;
	overflow: auto;
	max-height: 320px;
}
</style>
