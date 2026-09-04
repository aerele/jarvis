<template>
	<SettingsPane
		title="Connectors"
		:description="`Give ${agentName} access to other tools like GitHub, Linear or Stripe.`"
		:error="loadError"
	>
		<div v-if="loading && !loaded" class="grid place-items-center py-10">
			<JvSpinner />
		</div>

		<div v-else class="flex flex-col gap-8">
			<!-- ══════════════ Shared ══════════════ -->
			<div class="flex flex-col gap-3">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h3 class="text-base font-medium text-ink-gray-9">Shared</h3>
						<p class="text-p-sm text-ink-gray-6">Set by your admin.</p>
					</div>
					<Button
						v-if="isAdmin"
						variant="solid"
						iconLeft="plus"
						label="Add connector"
						@click="openAdd('Shared')"
					/>
				</div>

				<div
					v-if="!shared.length"
					class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-8 text-center"
				>
					<FeatherIcon name="link-2" class="size-6 text-ink-gray-4" />
					<div class="text-sm text-ink-gray-6">No shared connectors yet.</div>
				</div>
				<div v-else class="flex flex-col gap-2">
					<ConnectorRow
						v-for="row in shared"
						:key="row.name"
						:row="row"
						:can-manage="isAdmin"
						:acting="acting === row.name"
						@test="test(row)"
						@edit="openEdit(row)"
						@delete="confirmDelete(row)"
						@toggle="toggleEnabled(row, $event)"
					/>
				</div>
			</div>

			<!-- ══════════════ Mine ══════════════ -->
			<div class="flex flex-col gap-3">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h3 class="text-base font-medium text-ink-gray-9">Mine</h3>
						<p class="text-p-sm text-ink-gray-6">Only you can use these.</p>
					</div>
					<Button
						variant="solid"
						iconLeft="plus"
						label="Add connector"
						@click="openAdd('Personal')"
					/>
				</div>

				<div
					v-if="!mine.length"
					class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-8 text-center"
				>
					<FeatherIcon name="link-2" class="size-6 text-ink-gray-4" />
					<div class="text-sm text-ink-gray-6">
						No personal connectors yet. Add one to let {{ agentName }} use it in chat.
					</div>
				</div>
				<div v-else class="flex flex-col gap-2">
					<ConnectorRow
						v-for="row in mine"
						:key="row.name"
						:row="row"
						:can-manage="true"
						:acting="acting === row.name"
						@test="test(row)"
						@edit="openEdit(row)"
						@delete="confirmDelete(row)"
						@toggle="toggleEnabled(row, $event)"
					/>
				</div>
			</div>
		</div>

		<AddConnectorDialog
			v-model="addOpen"
			:scope="addScope"
			:allow-custom-urls="allowCustomUrls"
			:connector="editingRow"
			@saved="onSaved"
		/>
	</SettingsPane>
</template>

<script setup>
// MCP Connectors settings pane (MCP_CONNECTORS_PLAN.md P4). One per-user pane,
// two sections — copies PersonalisationSettings' two-section list idiom (rows
// with a Badge + Switch + ghost icon Buttons, an empty-state card per section,
// confirmDialog for delete, JvSpinner while loading) rather than inventing a
// new list shape.
//
// "Shared" rows are admin-managed (Jarvis Settings desk form owns the
// enabled/allow-custom-urls policy — this pane is deliberately lean); a plain
// user sees them read-only (no edit/delete/toggle) but MAY still press Test —
// connectors_api.test_connector is gated on read, not write, so any tenant
// user can run a live health probe on a Shared connector. "Mine" rows are
// fully owned by the caller. isAdmin reuses SettingsDialog's own gate
// (is_system_manager OR is_jarvis_admin) rather than inventing a second one.
import { computed, onMounted, ref } from "vue";
import { Button, FeatherIcon, confirmDialog, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import AddConnectorDialog from "@/components/settings/AddConnectorDialog.vue";
import ConnectorRow from "@/components/settings/ConnectorRow.vue";
import { deleteConnector, listConnectors, testConnector, updateConnector } from "@/api";
import { agentName } from "@/branding";
import { errMessage, errHtml } from "@/lib/errors";

const isAdmin = !!window.is_system_manager || !!window.is_jarvis_admin;

const loading = ref(false);
const loaded = ref(false);
const loadError = ref("");
const shared = ref([]);
const mine = ref([]);
const allowCustomUrls = ref(true);
const acting = ref(""); // row name mid Test/toggle, for the per-row spinner state

async function load() {
	loading.value = true;
	loadError.value = "";
	try {
		const res = await listConnectors();
		shared.value = res.shared || [];
		mine.value = res.mine || [];
		allowCustomUrls.value = !!res.allow_custom_urls;
		loaded.value = true;
	} catch (e) {
		loadError.value = errMessage(e, "Could not load connectors.");
	} finally {
		loading.value = false;
	}
}

// ── add / edit dialog ───────────────────────────────────────────────────────
const addOpen = ref(false);
const addScope = ref("Personal");
const editingRow = ref(null);

function openAdd(scope) {
	editingRow.value = null;
	addScope.value = scope;
	addOpen.value = true;
}
function openEdit(row) {
	editingRow.value = row;
	addOpen.value = true;
}
function onSaved() {
	load();
}

// ── row actions ──────────────────────────────────────────────────────────
async function test(row) {
	acting.value = row.name;
	try {
		const res = await testConnector(row.name);
		row.last_test_status = res && res.ok ? "Passed" : "Failed";
		row.last_test_at = new Date().toISOString();
		if (res && res.ok) {
			toast.success(`Connected, ${(res.tools || []).length} tools found`);
		} else {
			toast.error(errHtml({ message: (res && res.error && res.error.message) || "" }, "Test failed."));
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		acting.value = "";
	}
}

async function toggleEnabled(row, value) {
	const prev = row.enabled;
	row.enabled = value;
	acting.value = row.name;
	try {
		await updateConnector(row.name, { enabled: value ? 1 : 0 });
		toast.success(value ? "Connector enabled" : "Connector disabled");
	} catch (e) {
		row.enabled = prev;
		toast.error(errHtml(e));
	} finally {
		acting.value = "";
	}
}

function confirmDelete(row) {
	confirmDialog({
		title: "Delete this connector?",
		message: `${agentName} will no longer be able to use "${row.label}". This cannot be undone.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await deleteConnector(row.name);
				hideDialog();
				toast.success("Connector deleted");
				load();
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

onMounted(load);
</script>
