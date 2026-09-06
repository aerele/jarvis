<template>
	<div
		class="flex items-center gap-3 rounded-lg border p-3"
		:class="{ 'opacity-50': !row.enabled }"
	>
		<ConnectorLogo :preset="row.preset" :size="20" class="shrink-0 text-ink-gray-5" />
		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-1.5">
				<span class="truncate text-sm font-medium text-ink-gray-9">{{ row.label }}</span>
				<Tooltip :text="statusTip">
					<Badge variant="subtle" size="sm" :theme="statusTheme" :label="statusLabel" />
				</Tooltip>
			</div>
			<div class="truncate text-xs" :class="subtextClass">{{ subtext }}</div>
		</div>

		<Switch
			:modelValue="!!row.enabled"
			:disabled="!canManage || toggling"
			@update:modelValue="(v) => emit('toggle', v)"
		/>

		<!-- One next action per state (design: RowStates.dc.html). OAuth
		     connect/disconnect is a per-user action (design §6a: a Shared
		     connector is set up once by an admin, but each user runs their own
		     sign-in), so it lives outside the canManage-gated block below and is
		     never disabled for a non-admin viewing a Shared row. -->
		<Button
			v-if="rowState === 'setup-needed' && canManage"
			variant="subtle"
			label="Finish setup"
			@click="emit('edit')"
		/>
		<Button
			v-else-if="rowState === 'not-connected'"
			variant="subtle"
			label="Sign in"
			:loading="signingIn"
			loading-text="Waiting…"
			@click="doSignIn"
		/>
		<Button
			v-else-if="rowState === 'connected' && isOauth"
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
// Buttons) and PromotionStatusChip's Badge+Tooltip status idiom. Key rows and
// OAuth rows (auth_method "OAuth") share one 5-state model (design:
// RowStates.dc.html) - see rowState below.
import { computed, ref } from "vue";
import { Badge, Button, Switch, Tooltip, confirmDialog, toast } from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";
import { signIn } from "@/components/settings/oauthSignin";
import { disconnectOauth } from "@/api";
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

// ── unified row state ────────────────────────────────────────────────────
// The one if-chain each state's theme/label/tooltip/subtext/button used to
// repeat separately (and could drift). needs_static_client is checked ahead
// of oauth_configured because it's the authoritative "an admin must act"
// signal (spec-compliant client, MCP_OAUTH_CLIENT_DESIGN.md §8) - a row that
// needs a client id/secret always reads "Setup needed" even if
// oauth_configured happens to lag behind it.
const rowState = computed(() => {
	if (!props.row.enabled) return "disabled";
	if (isOauth.value) {
		if (props.row.oauth_connected) return "connected";
		if (props.row.needs_static_client) return "setup-needed";
		if (props.row.oauth_configured) return "not-connected";
		return "setup-needed";
	}
	if (props.row.last_test_status === "Passed") return "connected";
	if (props.row.last_test_status === "Failed") return "failed";
	return "not-tested";
});

const STATE_THEME = {
	disabled: "gray",
	connected: "green",
	"setup-needed": "orange",
	"not-connected": "gray",
	failed: "red",
	"not-tested": "gray",
};
const STATE_LABEL = {
	disabled: "Disabled",
	connected: "Connected",
	"setup-needed": "Setup needed",
	"not-connected": "Not connected",
	failed: "Failed",
	"not-tested": "Not tested",
};
const statusTheme = computed(() => STATE_THEME[rowState.value]);
const statusLabel = computed(() => STATE_LABEL[rowState.value]);

// last_test_at's "when" suffix, shared by the Failed tooltip and subtext.
const testWhen = computed(() =>
	props.row.last_test_at ? ` ${timeAgo(props.row.last_test_at)}` : ""
);

const statusTip = computed(() => {
	switch (rowState.value) {
		case "disabled":
			return "Turned off, won't be offered in chat.";
		case "connected":
			return isOauth.value
				? "You're connected. Only you can use this connection."
				: `Last test passed${testWhen.value}.`;
		case "setup-needed":
			return "Ask your admin to finish setup.";
		case "not-connected":
			return "Sign in to start using this connector.";
		case "failed":
			return `Last test failed${testWhen.value}.`;
		default:
			return "Run a test to confirm it's reachable.";
	}
});
// Every sign-in (dcr/static/Custom URL, GitHub included) shows where it signs in
// alongside the address (design §6's confused-deputy line, echoed here). One line
// either way, no new row. A Failed row shows the failure instead, in red.
const subtext = computed(() => {
	if (rowState.value === "failed") return `Last test failed${testWhen.value}.`;
	if (isOauth.value && props.row.signin_host) {
		return props.row.base_url
			? `${props.row.base_url} · Signs in at ${props.row.signin_host}`
			: `Signs in at ${props.row.signin_host}`;
	}
	return props.row.base_url || "";
});
const subtextClass = computed(() =>
	rowState.value === "failed" ? "text-ink-red-4" : "text-ink-gray-5"
);

// ── connect / disconnect ─────────────────────────────────────────────────
const signingIn = ref(false);
const disconnecting = ref(false);

async function doSignIn() {
	if (signingIn.value) return;
	signingIn.value = true;
	try {
		// signIn() returns a promise (with a .cancel() this row never uses -
		// there's no Cancel affordance here, only in AddConnectorDialog) that
		// resolves once the flow has a verdict; passing the row's own name
		// (not a factory) since the row already exists.
		const result = await signIn(props.row.name, { label: props.row.label, agentName });
		if (result.status === "connected") {
			emit("reload");
		} else if (result.status === "error") {
			toast.error(errHtml({ message: result.message }, "Could not sign in."));
		} else if (result.status === "closed") {
			toast.error("Sign-in window was closed.");
		} else if (result.status === "timeout") {
			toast.error("Sign-in took too long. Try again.");
		}
		// "navigated": this tab is leaving (popup was blocked) - nothing left to do.
	} finally {
		signingIn.value = false;
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
