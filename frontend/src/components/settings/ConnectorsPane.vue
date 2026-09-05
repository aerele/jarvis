<template>
	<SettingsPane
		title="Connectors"
		:description="`Give ${agentName} access to other tools like GitHub, Linear or Stripe.`"
	>
		<!-- Load failure keeps its own inline recovery rather than the pane-level
		     error slot, which is reserved for action errors (UsageAdminPane's
		     pattern) — showing the empty-state cards on a failed load would read
		     as "you have no connectors" instead of "we couldn't load them". -->
		<div v-if="loadError" class="flex flex-col items-center gap-3 py-12 text-center">
			<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">Could not load connectors.</span>
			<Button
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="loading"
				@click="load"
			/>
		</div>

		<div v-else-if="loading && !loaded" class="grid place-items-center py-10">
			<JvSpinner />
		</div>

		<div v-else class="flex flex-col gap-8">
			<!-- ══════════════ Shared ══════════════ -->
			<div class="flex flex-col gap-3">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h3 class="text-base font-semibold text-ink-gray-9">Shared</h3>
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
						:testing="testingRow === row.name"
						:toggling="togglingRow === row.name"
						@test="test(row)"
						@edit="openEdit(row)"
						@delete="confirmDelete(row)"
						@toggle="toggleEnabled(row, $event)"
						@reload="load"
					/>
				</div>
			</div>

			<!-- ══════════════ Mine ══════════════ -->
			<div class="flex flex-col gap-3">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h3 class="text-base font-semibold text-ink-gray-9">Mine</h3>
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
					<div class="text-sm text-ink-gray-6">No personal connectors yet.</div>
				</div>
				<div v-else class="flex flex-col gap-2">
					<ConnectorRow
						v-for="row in mine"
						:key="row.name"
						:row="row"
						:can-manage="true"
						:testing="testingRow === row.name"
						:toggling="togglingRow === row.name"
						@test="test(row)"
						@edit="openEdit(row)"
						@delete="confirmDelete(row)"
						@toggle="toggleEnabled(row, $event)"
						@reload="load"
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
import { errHtml } from "@/lib/errors";

const isAdmin = !!window.is_system_manager || !!window.is_jarvis_admin;

const loading = ref(false);
const loaded = ref(false);
const loadError = ref(false);
const shared = ref([]);
const mine = ref([]);
const allowCustomUrls = ref(true);
// Two independent per-row flags — a Test press only ever sets testingRow, a
// Switch flip only ever sets togglingRow, so neither control's spinner reads
// the other action's state.
const testingRow = ref("");
const togglingRow = ref("");

async function load() {
	loading.value = true;
	loadError.value = false;
	try {
		const res = await listConnectors();
		shared.value = res.shared || [];
		mine.value = res.mine || [];
		allowCustomUrls.value = !!res.allow_custom_urls;
		loaded.value = true;
	} catch (e) {
		loadError.value = true;
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
	testingRow.value = row.name;
	try {
		const res = await testConnector(row.name);
		row.last_test_status = res && res.ok ? "Passed" : "Failed";
		row.last_test_at = new Date().toISOString();
		if (res && res.ok) {
			const n = (res.tools || []).length;
			toast.success(`Connected, ${n} ${n === 1 ? "tool" : "tools"} found`);
		} else {
			toast.error(
				errHtml({ message: (res && res.error && res.error.message) || "" }, "Test failed.")
			);
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		testingRow.value = "";
	}
}

async function toggleEnabled(row, value) {
	const prev = row.enabled;
	row.enabled = value;
	togglingRow.value = row.name;
	try {
		await updateConnector(row.name, { enabled: value ? 1 : 0 });
		toast.success(value ? "Connector enabled" : "Connector disabled");
	} catch (e) {
		row.enabled = prev;
		toast.error(errHtml(e));
	} finally {
		togglingRow.value = "";
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

// ── OAuth return (design §7) ─────────────────────────────────────────────
// The provider round-trips the browser back to "...?settings=connectors&oauth=
// <name>" on success, or "...&oauth_error=expired|denied" on a failed/denied
// sign-in (AppShell's own ?settings= deep link opens this pane; that read
// already strips "settings" from the URL by the time this pane mounts, but
// leaves "oauth"/"oauth_error" alone). Read with URLSearchParams +
// history.replaceState, the same low-level idiom AppShell uses for that same
// one-time external-redirect deep link - NOT useRoute()/vue-router, whose
// reactive query would not reliably reflect a plain history.replaceState the
// router never drove.
//
// Reopening the row's own dialog (rather than a bare toast) matters: the row
// startOauthConnect created has no allowed actions yet and is not enabled -
// landing the user straight on the Test connection step (resetForEdit sees
// oauth_connected=true) is what gets them to a saved, enabled connector.
function consumeOauthReturn() {
	const params = new URLSearchParams(window.location.search);
	const name = params.get("oauth");
	const error = params.get("oauth_error");
	if (!name && !error) return null;
	params.delete("oauth");
	params.delete("oauth_error");
	const query = params.toString();
	const url = window.location.pathname + (query ? `?${query}` : "") + window.location.hash;
	history.replaceState(history.state, "", url);
	return { name, error };
}

onMounted(async () => {
	const oauthReturn = consumeOauthReturn();
	await load();
	if (!oauthReturn) return;
	if (oauthReturn.error) {
		toast.error("Sign-in didn't complete. Try again.");
		return;
	}
	const row = [...shared.value, ...mine.value].find((r) => r.name === oauthReturn.name);
	// Only land on the Edit dialog when the user can actually manage this row (an
	// own Personal row, or a Shared row and they're an admin). A non-admin who
	// just connected a Shared row cannot Save its allowed actions, so a bare
	// confirmation is the right ending for them.
	if (row && (row.scope !== "Shared" || isAdmin)) openEdit(row);
	else toast.success("Connected.");
});
</script>
