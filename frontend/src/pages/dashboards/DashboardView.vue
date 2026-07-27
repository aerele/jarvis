<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Dashboards', route: { name: 'DashboardsPage', hash: '#saved' } },
						{ label: detail ? detail.dashboard_title || detail.name : id },
					]"
				/>
			</template>
			<template #right-header>
				<template v-if="detail">
					<Dropdown v-if="moreOptions.length" :options="moreOptions">
						<!-- label → aria-label on icon-only buttons (frappe-ui) -->
						<Button icon="more-horizontal" variant="ghost" label="Dashboard actions" />
					</Dropdown>
					<!-- render theme: editors persist the pick, viewers restyle locally -->
					<Dropdown :options="themeOptions">
						<Button
							variant="ghost"
							:label="themeLabel(viewTheme)"
							iconLeft="droplet"
							iconRight="chevron-down"
						/>
					</Dropdown>
					<Button
						label="Discuss in chat"
						iconLeft="message-circle"
						@click="discussInChat"
					/>
					<Button
						v-if="detail.can_edit"
						label="Edit in builder"
						iconLeft="edit-2"
						@click="
							router.push({ name: 'DashboardsPage', query: { edit: detail.name } })
						"
					/>
					<Dropdown :options="exportOptions">
						<Button
							variant="solid"
							label="Export"
							iconRight="chevron-down"
							:loading="exporting"
						/>
					</Dropdown>
				</template>
			</template>
		</LayoutHeader>

		<!-- loading -->
		<div v-if="loading" class="flex flex-1 items-center justify-center">
			<LoadingIndicator class="size-5 text-ink-gray-5" />
		</div>

		<!-- §3.8 permission-blocked state -->
		<div
			v-else-if="blocked"
			class="flex flex-1 flex-col items-center justify-center gap-3 px-8"
		>
			<div
				class="flex flex-col items-center gap-3 rounded-md border border-dashed p-8 text-center"
			>
				<FeatherIcon name="alert-triangle" class="size-10 text-ink-red-4" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8"
						>No access to this dashboard</span
					>
					<span class="text-p-base text-ink-gray-6">
						It may be private or shared with a role you don't hold.
					</span>
				</div>
				<Button label="Back to dashboards" iconLeft="arrow-left" @click="goBack" />
			</div>
		</div>

		<!-- not found -->
		<div
			v-else-if="notFound"
			class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center"
		>
			<FeatherIcon name="bar-chart-2" class="size-10 text-ink-gray-4" />
			<div class="flex flex-col items-center gap-1">
				<span class="text-lg font-medium text-ink-gray-8">Dashboard not found</span>
				<span class="text-p-base text-ink-gray-6">It may have been deleted.</span>
			</div>
			<Button label="Back to dashboards" iconLeft="arrow-left" @click="goBack" />
		</div>

		<!-- §3.8 error + retry (transient load failure) -->
		<div
			v-else-if="error"
			class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center"
		>
			<ErrorMessage :message="error" />
			<Button label="Retry" :loading="loading" @click="load" />
		</div>

		<!-- the dashboard -->
		<div v-else-if="detail" class="min-h-0 flex-1 overflow-y-auto">
			<div class="flex flex-wrap items-center gap-2 px-5 pt-4">
				<h1 class="text-lg font-semibold text-ink-gray-9">
					{{ detail.dashboard_title || detail.name }}
				</h1>
				<Badge
					v-if="detail.dashboard_type"
					variant="subtle"
					theme="gray"
					:label="detail.dashboard_type"
				/>
				<Badge
					v-if="detail.scope === 'Role'"
					variant="subtle"
					theme="blue"
					:label="detail.target_role || 'Role'"
				/>
				<Badge
					v-else-if="detail.scope === 'Org'"
					variant="subtle"
					theme="blue"
					label="Everyone"
				/>
				<Badge v-else variant="subtle" theme="gray" label="Private" />
			</div>
			<p v-if="detail.description" class="px-5 pt-1 text-p-sm text-ink-gray-6">
				{{ detail.description }}
			</p>
			<div class="px-5 py-4">
				<DashboardCanvas
					ref="canvas"
					mode="view"
					:html="detail.html"
					:dashboard="{ name: detail.name }"
					:caps="caps"
					:theme="viewTheme"
				/>
			</div>
		</div>

		<SaveDashboardDialog
			v-model="shareOpen"
			share-only
			:caps="caps"
			:html="detail ? detail.html : ''"
			:sources="detail ? detail.sources || [] : []"
			:editing="detail"
			@saved="onShared"
		/>

		<!-- Hand-rolled so the destructive action is a RED solid button (§3.2);
		     frappe-ui's confirmDialog helper hardcodes a gray "Confirm". -->
		<Dialog v-model="deleteOpen" :options="deleteDialogOptions" />
	</div>
</template>

<script setup>
// DashboardView - /dashboards/:id, the read-only render of a saved dashboard.
// The canvas runs mode="view": data resolves by SOURCE NAME through
// run_dashboard_source (the server-stored spec is authoritative; whatever spec
// the html itself declares is ignored here), and the iframe grows to its
// reported height while this page scrolls. Export captures inside the iframe
// (html-to-image injected over postMessage - the CSP allows no external
// fetches) and assembles PNG/PDF downloads out here.
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import {
	Badge,
	Breadcrumbs,
	Button,
	Dialog,
	Dropdown,
	ErrorMessage,
	FeatherIcon,
	LoadingIndicator,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import { setChatPrefill } from "@/composables/chatPrefill";
import { getDashboard, getDashboardsCaps, deleteDashboard, saveDashboard } from "@/api/dashboards";
import { DEFAULT_THEME, THEME_OPTIONS, themeKey, themeLabel } from "@/lib/dashboardThemes";
import DashboardCanvas from "./DashboardCanvas.vue";
import SaveDashboardDialog from "./SaveDashboardDialog.vue";

const props = defineProps({
	id: { type: String, required: true },
});

const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const detail = ref(null);
const loading = ref(true);
const blocked = ref(false);
const notFound = ref(false);
const error = ref("");
const canvas = ref(null);
const exporting = ref(false);
const shareOpen = ref(false);

// Render theme: seeded from the saved dashboard; picking restyles immediately
// and (for editors) persists quietly — viewers just restyle their own view.
// Custom is EXCLUDED from the VIEW picker: opting a dashboard out of the enforced
// standard is a deliberate author/build-time choice, not a one-click view action
// (it would silently downgrade governance on a shared dashboard). It stays in the
// builder picker where the author owns the document.
const viewTheme = ref(DEFAULT_THEME);
const themeOptions = THEME_OPTIONS.filter((t) => t.key !== "custom").map((t) => ({
	label: t.label,
	onClick: () => pickTheme(t.key),
}));
async function pickTheme(key) {
	viewTheme.value = key;
	if (!(detail.value && detail.value.can_edit)) return;
	try {
		await saveDashboard({ name: detail.value.name, theme: themeLabel(key) });
		detail.value.theme = themeLabel(key);
	} catch (e) {
		toast.error(errMsg(e));
	}
}

// caps feed the share dialog's scope options; best-effort (share stays hidden
// until they land - view/export need nothing from them).
const caps = ref({ creatable_scopes: [], manageable_roles: [] });

async function load() {
	loading.value = true;
	blocked.value = false;
	notFound.value = false;
	error.value = "";
	try {
		detail.value = (await getDashboard(props.id)) || null;
		if (!detail.value) notFound.value = true;
		else viewTheme.value = themeKey(detail.value.theme);
	} catch (e) {
		if (e && (e.status === 403 || e.exc_type === "PermissionError")) blocked.value = true;
		else if (e && (e.status === 404 || e.exc_type === "DoesNotExistError"))
			notFound.value = true;
		else error.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

function goBack() {
	router.push({ name: "DashboardsPage", hash: "#saved" });
}

// ── header actions ───────────────────────────────────────────────────────────
const canShare = computed(
	() =>
		!!(detail.value && detail.value.can_edit && (caps.value.creatable_scopes || []).length > 1)
);

const moreOptions = computed(() => {
	const out = [];
	if (canShare.value)
		out.push({ label: "Share…", icon: "users", onClick: () => (shareOpen.value = true) });
	if (detail.value && detail.value.can_edit) {
		out.push({ label: "Delete", icon: "trash-2", theme: "red", onClick: confirmDelete });
	}
	return out;
});

const exportOptions = [
	{ label: "PNG image", onClick: () => doExport("png") },
	{ label: "PDF slides", onClick: () => doExport("pdf") },
];

async function doExport(format) {
	if (exporting.value || !canvas.value) return;
	exporting.value = true;
	try {
		await canvas.value.exportAs(format, detail.value && detail.value.dashboard_title);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		exporting.value = false;
	}
}

const deleteOpen = ref(false);
function confirmDelete() {
	deleteOpen.value = true;
}
const deleteDialogOptions = computed(() => {
	const title = (detail.value && detail.value.dashboard_title) || props.id;
	return {
		title: "Delete dashboard?",
		message: `Delete “${title}”? This can't be undone.`,
		actions: [
			{
				label: "Delete dashboard",
				variant: "solid",
				theme: "red",
				onClick: async ({ close }) => {
					try {
						await deleteDashboard(props.id);
						close();
						toast.success("Dashboard deleted");
						goBack();
					} catch (e) {
						toast.error(errMsg(e));
					}
				},
			},
		],
	};
});

function onShared(fresh) {
	if (fresh && fresh.name) detail.value = fresh;
	toast.success("Sharing updated");
}

// Hand the dashboard to the main chat as viewing context (api.js#sendMessage
// forwards `context` because it carries a doctype) and let ChatView's prefill
// consumption start a fresh conversation + auto-send.
function discussInChat() {
	const title = (detail.value && detail.value.dashboard_title) || props.id;
	setChatPrefill({
		text: `Let's discuss the dashboard "${title}" — what does it show and what stands out?`,
		autoSend: true,
		context: { doctype: "Jarvis Dashboard", name: props.id },
	});
	router.push("/");
}

onMounted(() => {
	load();
	getDashboardsCaps()
		.then((c) => {
			if (c) caps.value = { ...caps.value, ...c };
		})
		.catch(() => {});
});
</script>
