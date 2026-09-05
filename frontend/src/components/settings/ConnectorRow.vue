<template>
	<div class="flex items-center gap-3 rounded-lg border p-3">
		<ConnectorLogo :preset="row.preset" :size="20" class="shrink-0 text-ink-gray-5" />
		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-1.5">
				<span class="truncate text-sm font-medium text-ink-gray-9">{{ row.label }}</span>
				<Tooltip :text="statusTip">
					<Badge variant="subtle" size="sm" :theme="statusTheme" :label="statusLabel" />
				</Tooltip>
			</div>
			<div class="truncate text-xs text-ink-gray-5">{{ subtext }}</div>
		</div>

		<Switch
			:modelValue="!!row.enabled"
			:disabled="!canManage || toggling"
			@update:modelValue="(v) => emit('toggle', v)"
		/>

		<!-- OAuth connect/disconnect is a per-user action (design §6a: a Shared
		     connector is set up once by an admin, but each user runs their own
		     Connect), so it lives outside the canManage-gated block below and is
		     never disabled for a non-admin viewing a Shared row. Nothing renders
		     here when the row isn't set up yet (oauth_configured false) - the
		     status badge's tooltip already tells the user to ask their admin. -->
		<Button
			v-if="isOauth && row.oauth_configured && !row.oauth_connected"
			variant="subtle"
			label="Connect"
			:loading="connecting"
			@click="doConnect"
		/>
		<Button
			v-else-if="isOauth && row.oauth_connected"
			variant="ghost"
			icon="log-out"
			:loading="disconnecting"
			:tooltip="'Disconnect'"
			@click="doDisconnect"
		/>

		<Button
			variant="ghost"
			icon="refresh-cw"
			:loading="testing"
			:tooltip="'Test connection'"
			@click="emit('test')"
		/>
		<template v-if="canManage">
			<Button variant="ghost" icon="edit-2" :tooltip="'Edit'" @click="emit('edit')" />
			<Button
				variant="ghost"
				theme="red"
				icon="trash-2"
				:tooltip="'Delete'"
				@click="emit('delete')"
			/>
		</template>
	</div>
</template>

<script setup>
// One connector row - shared by ConnectorsPane's "Shared" and "Mine" lists.
// Copies PersonalisationSettings' row idiom (Badge + Switch + ghost icon
// Buttons) and PromotionStatusChip's Badge+Tooltip status idiom. Key rows keep
// MCP_CONNECTORS_PLAN.md's original three-state status (Connected / Failed /
// Disabled / Not tested); OAuth rows (auth_method "OAuth") get their own
// per-user status per OAUTH_CONNECTORS_DESIGN.md §6a - see statusLabel below.
import { computed, ref } from "vue";
import { Badge, Button, Switch, Tooltip, confirmDialog, toast } from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";
import { connectOauth, disconnectOauth } from "@/api";
import { agentName } from "@/branding";
import { errHtml } from "@/lib/errors";
import { timeAgo } from "@/utils/datetime";

const props = defineProps({
	row: { type: Object, required: true },
	// Shared rows are read-only for a non-admin: Test stays available (the
	// backend gates it on read, not write) but the toggle/edit/delete actions
	// are hidden entirely rather than shown disabled. Connect/Disconnect are
	// NOT gated on this - see the template comment above.
	canManage: { type: Boolean, default: true },
	// Split so flipping the Switch never spins the Test button and vice versa
	// (each control's :loading/:disabled reads only its own action's flag) -
	// mirrors PersonalisationSettings' rowActing, which likewise only ever
	// disables its own Switch and never leaks into another control.
	testing: { type: Boolean, default: false },
	toggling: { type: Boolean, default: false },
});
const emit = defineEmits(["test", "edit", "delete", "toggle", "reload"]);

const isOauth = computed(() => props.row.auth_method === "OAuth");

// `enabled` means the same thing for both auth methods (won't be offered in
// chat), so it's checked first for OAuth rows too, same as the key-row logic
// below it - a disabled OAuth row never shows a green "Connected" badge next
// to an off Switch.
const statusTheme = computed(() => {
	if (!props.row.enabled) return "gray";
	if (isOauth.value) return props.row.oauth_connected ? "green" : "gray";
	if (props.row.last_test_status === "Passed") return "green";
	if (props.row.last_test_status === "Failed") return "red";
	return "gray";
});
// needs_static_client (spec-compliant client, MCP_OAUTH_CLIENT_DESIGN.md §8)
// is the authoritative "an admin must act" signal - checked ahead of
// oauth_configured so a row that needs a client id/secret always reads
// "Setup needed" even if oauth_configured happens to lag behind it.
const statusLabel = computed(() => {
	if (!props.row.enabled) return "Disabled";
	if (isOauth.value) {
		if (props.row.oauth_connected) return "Connected";
		if (props.row.needs_static_client) return "Setup needed";
		if (props.row.oauth_configured) return "Not connected";
		return "Setup needed";
	}
	if (props.row.last_test_status === "Passed") return "Connected";
	if (props.row.last_test_status === "Failed") return "Failed";
	return "Not tested";
});
const statusTip = computed(() => {
	if (!props.row.enabled) return "Turned off, won't be offered in chat.";
	if (isOauth.value) {
		if (props.row.oauth_connected)
			return "You're connected. Only you can use this connection.";
		if (props.row.needs_static_client) return "Ask your admin to finish setup.";
		if (props.row.oauth_configured) return "Sign in to start using this connector.";
		return "Ask your admin to finish setup.";
	}
	const when = props.row.last_test_at ? ` ${timeAgo(props.row.last_test_at)}` : "";
	if (props.row.last_test_status === "Passed") return `Last test passed${when}.`;
	if (props.row.last_test_status === "Failed") return `Last test failed${when}.`;
	return "Run a test to confirm it's reachable.";
});
// Every discovered sign-in (dcr/static/Custom URL) shows where it signs in
// alongside the address (design §6's confused-deputy line, echoed here) -
// only a Connected App (GitHub) skips it, since its sign-in host is implied
// by the brand and showing it would just be noise. One line either way, no
// new row.
const subtext = computed(() => {
	if (isOauth.value && props.row.auth_class !== "connected_app" && props.row.signin_host) {
		return props.row.base_url
			? `${props.row.base_url} · Signs in at ${props.row.signin_host}`
			: `Signs in at ${props.row.signin_host}`;
	}
	return props.row.base_url || "";
});

// ── connect / disconnect ─────────────────────────────────────────────────
const connecting = ref(false);
const disconnecting = ref(false);

async function doConnect() {
	if (connecting.value) return;
	connecting.value = true;
	try {
		const res = await connectOauth(props.row.name);
		if (res && res.ok && res.url) {
			window.location.href = res.url;
			return;
		}
		toast.error(
			errHtml(
				{ message: (res && res.error && res.error.message) || "" },
				"Could not connect."
			)
		);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		connecting.value = false;
	}
}

function doDisconnect() {
	confirmDialog({
		title: "Disconnect this app?",
		message: `${agentName} will no longer be able to use "${props.row.label}" until you connect again.`,
		onConfirm: async ({ hideDialog }) => {
			disconnecting.value = true;
			try {
				await disconnectOauth(props.row.name);
				hideDialog();
				toast.success("Disconnected");
				emit("reload");
			} catch (e) {
				toast.error(errHtml(e));
			} finally {
				disconnecting.value = false;
			}
		},
	});
}
</script>
