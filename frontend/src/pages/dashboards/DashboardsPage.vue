<template>
	<div class="flex h-full flex-col overflow-hidden">
		<!-- friendly no-access state: get_dashboards_caps rejected with a real 403
		     (TriggersPage probe precedent - transient failures retry, never block) -->
		<template v-if="accessDenied">
			<div class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
				<FeatherIcon name="bar-chart-2" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8"
						>No access to Dashboards</span
					>
					<span class="text-p-base text-ink-gray-6">
						Ask your {{ agentName }} admin for access to dashboards.
					</span>
				</div>
			</div>
		</template>

		<template v-else>
			<!-- THE LayoutHeader for /dashboards (both tabs; SavedDashboardsTab's
			     ListPage runs show-header=false) -->
			<LayoutHeader>
				<template #left-header>
					<Breadcrumbs
						:items="[{ label: 'Dashboards', route: { name: 'DashboardsPage' } }]"
					/>
				</template>
				<template #right-header>
					<Button
						v-if="activeTab === 'saved'"
						variant="solid"
						label="New dashboard"
						iconLeft="plus"
						@click="newDashboard"
					/>
				</template>
			</LayoutHeader>

			<TabBar
				class="shrink-0"
				:tabs="TABS"
				:model-value="activeTab"
				@update:model-value="setTab"
			/>

			<!-- ============ Builder tab: canvas over chat, drag-split ============ -->
			<!-- self-hosted benches have no gateway canvas route, so chat can't
			     produce a rendered dashboard - say so instead of an eternal empty
			     canvas (caps.canvas_available from get_dashboards_caps). -->
			<div
				v-if="activeTab === 'builder' && caps.canvas_available === false"
				class="flex items-start gap-2 border-b bg-surface-amber-1 px-4 py-2"
			>
				<FeatherIcon
					name="alert-triangle"
					class="mt-0.5 size-4 shrink-0 text-ink-amber-3"
				/>
				<span class="text-sm text-ink-gray-7">
					Live canvas rendering isn't available on this deployment, so chat can't draw a
					dashboard here. You can still open and view saved dashboards.
				</span>
			</div>
			<div
				v-if="activeTab === 'builder'"
				ref="builderEl"
				class="flex min-h-0 flex-1 flex-col"
			>
				<!-- canvas pane (the surface's one solid action lives here) -->
				<div
					class="flex min-h-0 flex-1 flex-col"
					:class="resizing ? 'pointer-events-none select-none' : ''"
				>
					<div
						class="flex shrink-0 items-center justify-between gap-2 border-b px-4 py-2"
					>
						<div class="flex min-w-0 items-center gap-2">
							<span class="text-base font-semibold text-ink-gray-9">Canvas</span>
							<!-- informational (which dashboard is loaded), not a dirty
							     warning - so gray, not orange (§1.2 hue = meaning) -->
							<Badge
								v-if="editingDetail"
								theme="gray"
								variant="subtle"
								:label="`Editing ${
									editingDetail.dashboard_title || editingDetail.name
								}`"
							/>
						</div>
						<div class="flex shrink-0 items-center gap-3">
							<router-link
								v-if="savedName"
								:to="{ name: 'DashboardView', params: { id: savedName } }"
								class="text-sm text-ink-blue-link"
							>
								View dashboard
							</router-link>
							<!-- named render theme - the dashboard's look, not the app's -->
							<Dropdown :options="themeOptions">
								<Button
									variant="ghost"
									:label="themeLabel(builderTheme)"
									iconLeft="droplet"
									iconRight="chevron-down"
								/>
							</Dropdown>
							<Button
								variant="solid"
								label="Save dashboard"
								:disabled="!builderHtml"
								@click="openSave"
							/>
						</div>
					</div>
					<DashboardCanvas
						class="min-h-0 flex-1"
						mode="builder"
						:html="builderHtml"
						:caps="caps"
						:theme="builderTheme"
						@sources="(s) => (detectedSources = s)"
					/>
				</div>

				<!-- drag divider (Sidebar's resize pattern, rotated). While dragging,
				     the canvas wrapper above goes pointer-events-none so the iframe
				     can't swallow the mousemoves. -->
				<div
					class="group relative z-10 flex h-2.5 shrink-0 cursor-row-resize items-center justify-center"
					role="separator"
					aria-orientation="horizontal"
					title="Drag to resize · double-click to reset"
					@mousedown.prevent="startResize"
					@dblclick="resetSplit"
				>
					<span
						class="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 transition-colors"
						:class="
							resizing
								? 'bg-surface-gray-4'
								: 'bg-transparent group-hover:bg-surface-gray-4'
						"
					/>
					<span
						class="relative h-1 w-7 rounded-full bg-surface-gray-4 transition-opacity"
						:class="resizing ? 'opacity-100' : 'opacity-30 group-hover:opacity-100'"
					/>
				</div>

				<DashboardChatPane
					ref="chatPane"
					class="shrink-0 border-t"
					:style="{ height: chatPct + '%' }"
					:caps="caps"
					:theme="builderTheme"
					:editing-name="editingDetail ? editingDetail.name : ''"
					@canvas="onCanvas"
					@reset="resetBuilder"
				/>
			</div>

			<!-- ============ Saved tab ============ -->
			<SavedDashboardsTab v-else class="min-h-0 flex-1" />

			<SaveDashboardDialog
				v-model="saveOpen"
				:caps="caps"
				:html="builderHtml"
				:sources="detectedSources"
				:editing="editingDetail"
				:conversation="chatConv"
				:theme="builderTheme"
				@saved="onSaved"
				@fix-in-chat="fixInChat"
			/>
		</template>
	</div>
</template>

<script setup>
// DashboardsPage - the routed component for /dashboards: hash-synced tab shell
// (TriggersPage precedent; no hash or "#builder" = Builder, "#saved" = Saved)
// plus the single get_dashboards_caps probe that feeds both tabs. The Builder
// tab is the core UX: the sandboxed canvas on top, the assistant chat below,
// split by a draggable divider (persisted %). The chat's canvas frames pull
// the agent's html artifact onto the canvas; Save opens the scope/title
// dialog; ?edit=<name> seeds the canvas from a saved dashboard for editing.
// Probe failures follow the TriggersPage rule: a genuine 403 shows the
// no-access state; a transient 500/network blip retries once and otherwise
// proceeds with default caps rather than blocking an authorized user.
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import { Badge, Breadcrumbs, Button, Dropdown, FeatherIcon, toast } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import TabBar from "@/components/list/TabBar.vue";
import { session } from "@/data/session";
import { getCanvas } from "@/api";
import { agentName } from "@/branding";
import { getDashboardsCaps, getDashboard } from "@/api/dashboards";
import { DEFAULT_THEME, THEME_OPTIONS, themeKey, themeLabel } from "@/lib/dashboardThemes";
import DashboardCanvas from "./DashboardCanvas.vue";
import DashboardChatPane from "./DashboardChatPane.vue";
import SavedDashboardsTab from "./SavedDashboardsTab.vue";
import SaveDashboardDialog from "./SaveDashboardDialog.vue";

const route = useRoute();
const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const TABS = [
	{ label: "Builder", value: "builder" },
	{ label: "Saved", value: "saved" },
];

// caps flow down reactively - the save dialog's scope options appear once the
// probe lands; the default keeps a plain User-scoped save path.
const caps = ref({
	creatable_scopes: ["User"],
	manageable_roles: [],
	max_sources: 0,
	max_html_chars: 0,
	max_rows: 0,
	canvas_available: false,
});
const accessDenied = ref(false);

// ── hash-synced tabs (TriggersPage precedent) ────────────────────────────────
const activeTab = ref("builder");
function applyHash() {
	// tolerate suffixed forms like "#saved?x=1"
	const h = (route.hash || "").replace(/^#/, "").split("?")[0];
	activeTab.value = h === "saved" ? "saved" : "builder";
}
function setTab(v) {
	if (v === activeTab.value) return;
	activeTab.value = v;
	router.push({ hash: v === "builder" ? "" : `#${v}`, query: route.query });
}
applyHash();
// back/forward restores the tab (guard to this route so other pages' hashes
// are ignored - the SkillsPage rule)
watch(
	() => route.hash,
	() => {
		if (route.name === "DashboardsPage") applyHash();
	}
);

// ── builder state ────────────────────────────────────────────────────────────
const builderHtml = ref("");
const editingDetail = ref(null); // full get_dashboard detail while editing
const savedName = ref(""); // last save's name → the "View dashboard" link
const detectedSources = ref([]); // parsed #jarvis-sources (DashboardCanvas emit)
const saveOpen = ref(false);
const chatPane = ref(null);
// Named render theme; "Jarvis" (the app's design language) unless picked or
// seeded from the edited dashboard.
const builderTheme = ref(DEFAULT_THEME);
const themeOptions = THEME_OPTIONS.map((t) => ({
	label: t.label,
	onClick: () => (builderTheme.value = t.key),
}));

// Same per-user keys DashboardChatPane persists under (vueuse syncs same-
// document instances) — the page seeds/clears them around edit/new so the chat
// pane resumes the right thread and data mode instead of a stale sticky one.
const chatConv = useStorage(`jarvis-dash-conv-${session.user || "anon"}`, "");
const dashDataMode = useStorage(`jarvis-dash-datamode-${session.user || "anon"}`, "auto");

// Chat drew/updated an artifact: pick the LAST html item and pull its
// render-ready content onto the canvas.
async function onCanvas({ message_id, items }) {
	const htmlItem = [...(items || [])].reverse().find((it) => it && it.type === "html");
	if (!htmlItem || !message_id) return;
	try {
		const r = await getCanvas(message_id, htmlItem.name, 0);
		const content = r && (r.content || r.data_url);
		if (content) builderHtml.value = content;
	} catch (e) {
		toast.error(errMsg(e));
	}
}

function openSave() {
	if (!builderHtml.value) return;
	saveOpen.value = true;
}

// A theme-rejected save hands its violations back to the model: close the dialog
// and post the message into the builder chat, so the agent regenerates on-theme
// without the user relaying CSS jargon (P0-2).
function fixInChat({ text }) {
	saveOpen.value = false;
	if (chatPane.value && chatPane.value.sendText) chatPane.value.sendText(text);
}

function onSaved(detail) {
	savedName.value = (detail && detail.name) || "";
	// keep editing the row we just saved - the next Save is "Save changes"
	if (detail && detail.name) editingDetail.value = detail;
	toast.success("Dashboard saved");
}

// "New dashboard" (Saved tab header) - fresh chat + empty canvas on Builder.
// One navigation clears the tab hash AND any ?edit seed together (resetBuilder
// + setTab separately would race on route.query).
function newDashboard() {
	chatConv.value = ""; // pane isn't mounted on the Saved tab; clear before it is
	dashDataMode.value = "auto";
	builderHtml.value = "";
	editingDetail.value = null;
	savedName.value = "";
	detectedSources.value = [];
	builderTheme.value = DEFAULT_THEME;
	activeTab.value = "builder";
	router.push({ hash: "", query: {} });
}

// Also fired by the chat pane's own "New chat" (emit("reset")).
function resetBuilder() {
	chatConv.value = "";
	dashDataMode.value = "auto";
	builderHtml.value = "";
	editingDetail.value = null;
	savedName.value = "";
	detectedSources.value = [];
	builderTheme.value = DEFAULT_THEME;
	if (route.query.edit) router.replace({ query: {}, hash: route.hash });
}

// ?edit=<name> deep-link: seed the canvas + save dialog from a saved dashboard.
// Also resume the conversation that built it (so the agent has memory of the
// document) and seed the data-mode from its derived type, so an edit session
// never silently drifts onto/converts the wrong dashboard.
async function loadEdit(name) {
	try {
		const d = await getDashboard(name);
		if (d && d.name) {
			builderHtml.value = d.html || "";
			editingDetail.value = d;
			savedName.value = d.name;
			builderTheme.value = themeKey(d.theme);
			// resume the build thread, or a fresh one — never the stale sticky
			// conversation left over from editing a different dashboard.
			chatConv.value = d.source_conversation || "";
			dashDataMode.value = d.dashboard_type === "Connected" ? "live" : "static";
		}
	} catch (e) {
		toast.error(errMsg(e));
	}
}

// ── the drag-split (Sidebar's resize machinery, vertical) ────────────────────
const builderEl = ref(null);
const _split = useStorage("jarvis-dash-split", 40);
const clampPct = (n) => Math.min(70, Math.max(20, Math.round(Number(n) || 40)));
const chatPct = computed({
	get: () => clampPct(_split.value),
	set: (v) => (_split.value = clampPct(v)),
});
const resizing = ref(false);
let startY = 0;
let startPct = 40;
let containerH = 1;

function startResize(e) {
	if (e.button !== 0) return;
	resizing.value = true;
	startY = e.clientY;
	startPct = chatPct.value;
	containerH = (builderEl.value && builderEl.value.getBoundingClientRect().height) || 1;
	window.addEventListener("mousemove", onResize);
	window.addEventListener("mouseup", stopResize);
	document.body.style.userSelect = "none";
	document.body.style.cursor = "row-resize";
}
function onResize(e) {
	chatPct.value = startPct + ((startY - e.clientY) / containerH) * 100;
}
function stopResize() {
	if (!resizing.value) return;
	resizing.value = false;
	window.removeEventListener("mousemove", onResize);
	window.removeEventListener("mouseup", stopResize);
	document.body.style.userSelect = "";
	document.body.style.cursor = "";
}
function resetSplit() {
	chatPct.value = 40;
}
onBeforeUnmount(stopResize);

// ── caps probe (403 vs transient, TriggersPage pattern) ──────────────────────
function isPermissionError(e) {
	return !!(e && (e.status === 403 || e.exc_type === "PermissionError"));
}

onMounted(async () => {
	let fresh = null;
	try {
		fresh = await getDashboardsCaps();
	} catch (e) {
		if (isPermissionError(e)) {
			accessDenied.value = true;
			return;
		}
		// transient (500/network) - retry once before giving up
		await new Promise((r) => setTimeout(r, 1000));
		try {
			fresh = await getDashboardsCaps();
		} catch (e2) {
			if (isPermissionError(e2)) {
				accessDenied.value = true;
				return;
			}
			// still transient - keep the defaults instead of blocking
			console.warn("get_dashboards_caps failed twice; keeping default caps", e2);
		}
	}
	if (fresh) caps.value = { ...caps.value, ...fresh };

	const edit = typeof route.query.edit === "string" ? route.query.edit : "";
	if (edit) loadEdit(edit);
});
</script>
