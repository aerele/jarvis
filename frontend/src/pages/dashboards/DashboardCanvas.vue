<template>
	<div class="relative flex h-full min-h-0 w-full flex-col">
		<!-- §3.8 empty state - builder: "ask in chat"; view: "this is empty" -->
		<div
			v-if="!html"
			class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center"
		>
			<FeatherIcon name="bar-chart-2" class="size-7.5 text-ink-gray-5" />
			<div class="flex flex-col items-center gap-1">
				<span class="text-lg font-medium text-ink-gray-8">
					{{
						mode === "builder"
							? "Nothing on the canvas yet"
							: "This dashboard is empty"
					}}
				</span>
				<span class="text-p-base text-ink-gray-6">
					{{
						mode === "builder"
							? "Ask for a dashboard in the chat below."
							: "It has no saved content to show."
					}}
				</span>
			</div>
		</div>

		<!-- §3.8 error + retry (srcdoc assembly / echarts chunk load failed) -->
		<div
			v-else-if="buildError"
			class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center"
		>
			<ErrorMessage :message="buildError" />
			<Button label="Retry" :loading="building" @click="rebuild" />
		</div>

		<template v-else>
			<!-- sandbox: allow-scripts ONLY (no allow-same-origin, no allow-popups) -
			     paired with the srcdoc CSP (no network, inline-only) the dashboard
			     runs fully isolated; all data arrives over postMessage. -->
			<iframe
				ref="frame"
				:srcdoc="doc"
				sandbox="allow-scripts"
				class="w-full border-0"
				:class="mode === 'view' ? '' : 'min-h-0 flex-1'"
				:style="mode === 'view' ? { height: frameH + 'px' } : null"
				title="Dashboard canvas"
			/>
			<div
				v-if="loading"
				class="absolute inset-0 flex items-center justify-center bg-surface-white/60"
			>
				<JvSpinner />
			</div>
		</template>
	</div>
</template>

<script setup>
// DashboardCanvas - the sandboxed dashboard surface, shared by the builder
// (mode="builder": fixed pane, sources executed ad-hoc via call_tool from the
// spec the HTML itself declares) and the saved view page (mode="view": iframe
// grows to its content, sources executed by name via run_dashboard_source -
// the SERVER-stored spec is authoritative and the frame's spec is ignored).
// The srcdoc is assembled by lib/dashboardSrcdoc (CSP + bridge runtime); the
// echarts source only loads (dynamic ?raw import, its own chunk) when the
// html actually references echarts. The named dashboard theme (--jd-* vars)
// owns the canvas look; switching it rebuilds the document.
import { ref, watch, onMounted, onBeforeUnmount } from "vue";
import { Button, ErrorMessage, FeatherIcon } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { buildSrcdoc, parseSourcesBlock } from "@/lib/dashboardSrcdoc";
import { THEMES, DEFAULT_THEME, themeKey } from "@/lib/dashboardThemes";
import { loadCaptureLib, downloadPng, downloadPdf } from "@/lib/dashboardExport";
import { runDashboardSource, callDashboardTool } from "@/api/dashboards";
import { errMessage as errMsg } from "@/lib/errors";

const props = defineProps({
	mode: { type: String, default: "builder" }, // "builder" | "view"
	html: { type: String, default: "" },
	dashboard: { type: Object, default: null }, // view mode: {name} at minimum
	caps: { type: Object, default: () => ({}) },
	// Named render theme (lib/dashboardThemes key or DocType label). The canvas
	// look belongs to the dashboard, not the app shell - app dark mode does not
	// restyle it, the picker does.
	theme: { type: String, default: DEFAULT_THEME },
});

// sources: the parsed #jarvis-sources list (save-dialog preview + payload);
// state: "empty" | "loading" | "ready" | "error" for hosts that care.
const emit = defineEmits(["sources", "state"]);

const frame = ref(null);
const doc = ref("");
const loading = ref(false);
const building = ref(false);
const buildError = ref("");
const frameH = ref(480); // view mode: grown by the iframe's height frames

function postToFrame(msg) {
	const fw = frame.value && frame.value.contentWindow;
	if (fw) fw.postMessage({ jarvis: 1, ...msg }, "*");
}

// ── srcdoc assembly ──────────────────────────────────────────────────────────
let readyTimer = null;
async function rebuild() {
	if (!props.html) {
		doc.value = "";
		emit("state", "empty");
		return;
	}
	building.value = true;
	buildError.value = "";
	loading.value = true;
	emit("state", "loading");
	try {
		let echartsSource = "";
		if (/\becharts\b/i.test(props.html)) {
			// Filesystem-relative ?raw import: echarts' exports map blocks bare
			// deep imports; the raw source ships as its own lazy chunk.
			const mod = await import("../../../node_modules/echarts/dist/echarts.min.js?raw");
			echartsSource = (mod && mod.default) || "";
		}
		doc.value = buildSrcdoc(props.html, {
			echartsSource,
			theme: THEMES[themeKey(props.theme)],
		});
		// Safety valve: if the frame never posts ready (user script threw before
		// our runtime's DOMContentLoaded, etc.) don't spin forever.
		clearTimeout(readyTimer);
		readyTimer = setTimeout(() => (loading.value = false), 8000);
	} catch (e) {
		buildError.value = errMsg(e);
		loading.value = false;
		emit("state", "error");
	} finally {
		building.value = false;
	}
}

watch(
	() => props.html,
	(h) => {
		emit("sources", parseSourcesBlock(h));
		rebuild();
	},
	{ immediate: true }
);

// Theme switches rebuild the document (charts re-init against the new
// palette) - a full reload is simpler and more reliable than live restyling.
watch(
	() => props.theme,
	() => rebuild()
);

// ── data bridge ──────────────────────────────────────────────────────────────
// run_report results resolve inside the iframe as {columns, rows}; query/
// get_list results resolve as the plain rows array (documented in RUNTIME_JS).
function dataPayload(data) {
	if (data && typeof data === "object" && !Array.isArray(data)) {
		if (data.columns) return { columns: data.columns, rows: data.rows || [] };
		if (Array.isArray(data.rows)) return data.rows;
	}
	return data;
}

async function handleData(d) {
	let reply;
	try {
		// call_tool dispatches kwargs: query takes its DSL object under the
		// `spec` kwarg; get_list/run_report take the object AS their kwargs.
		const args = d.tool === "query" ? { spec: d.spec } : d.spec;
		const env =
			props.mode === "view"
				? await runDashboardSource(props.dashboard && props.dashboard.name, d.name)
				: await callDashboardTool(d.tool, args);
		if (env && env.ok) {
			reply = { ok: true, rows: dataPayload(env.data) };
		} else {
			const err = (env && env.error) || {};
			reply = {
				ok: false,
				error: {
					code: err.code || "InternalError",
					message: err.message || "Couldn't load this data",
				},
			};
		}
	} catch (e) {
		// Thrown (non-envelope) server error - map onto the bridge's codes.
		const code =
			e && (e.status === 403 || e.exc_type === "PermissionError")
				? "PermissionError"
				: e && (e.status === 404 || e.exc_type === "DoesNotExistError")
				? "NotFound"
				: "InternalError";
		reply = { ok: false, error: { code, message: errMsg(e) } };
	}
	// Data failures render per-widget INSIDE the iframe (jarvis.renderError) -
	// never a toast out here.
	postToFrame({ type: "data:result", id: d.id, ...reply });
}

// ── export (lib injection + downloads live in lib/dashboardExport) ───────────
const pendingExports = {};
let exportSeq = 0;

async function exportAs(format, title) {
	const lib = await loadCaptureLib();
	const id = "x" + ++exportSeq;
	const result = new Promise((resolve, reject) => {
		pendingExports[id] = {
			resolve,
			reject,
			timer: setTimeout(() => {
				delete pendingExports[id];
				reject(new Error("Export timed out"));
			}, 60000),
		};
	});
	// pdf = the slide deck path (section.slide per page); png = one full-body shot
	postToFrame({
		type: "export",
		id,
		format: format === "pdf" ? "slides" : "png",
		lib,
		pixelRatio: 2,
	});
	const images = await result;
	if (format === "pdf") await downloadPdf(images, title);
	else downloadPng(images, title);
}
// rebuild() is exposed for hosts that hide this component with `v-show`: the
// iframe keeps loading while `display:none`, so charts initialise against a
// 0x0 container and ECharts does not self-heal when it reappears. The builder
// page re-drives it when the canvas becomes visible again.
defineExpose({ exportAs, rebuild });

// ── frame messages (validated: our iframe's window + the jarvis:1 stamp) ─────
function onMessage(e) {
	const fw = frame.value && frame.value.contentWindow;
	if (!fw || e.source !== fw) return;
	const d = e.data;
	if (!d || d.jarvis !== 1) return;
	if (d.type === "ready") {
		clearTimeout(readyTimer);
		loading.value = false;
		emit("state", "ready");
	} else if (d.type === "height") {
		if (props.mode === "view") frameH.value = Math.max(480, Math.ceil(d.height || 0));
	} else if (d.type === "data") {
		handleData(d);
	} else if (d.type === "export:result") {
		const p = pendingExports[d.id];
		if (!p) return;
		delete pendingExports[d.id];
		clearTimeout(p.timer);
		if (d.ok) p.resolve(d.images || []);
		else {
			const err = new Error((d.error && d.error.message) || "Export failed");
			err.code = (d.error && d.error.code) || "CaptureFailed";
			p.reject(err);
		}
	}
}

onMounted(() => window.addEventListener("message", onMessage));
onBeforeUnmount(() => {
	window.removeEventListener("message", onMessage);
	clearTimeout(readyTimer);
	for (const id of Object.keys(pendingExports)) {
		clearTimeout(pendingExports[id].timer);
		delete pendingExports[id];
	}
});
</script>
