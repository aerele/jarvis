<!-- The Admin tab's ONE access control (jarvis#1062).

     It replaced a two-step flow: a role picker with its own "Save roles" button,
     and no way at all to grant a single named person. Now roles and users are
     edited together and saved once, because they are two halves of one
     statement - moving somebody from a role grant to a named grant used to mean
     two saves with an unintended access state in between.

     Access is DENY BY DEFAULT. An empty pair does not mean "everyone"; it means
     admins only, and the copy here says so rather than leaving the admin to
     infer the inversion from a blank list.

     Extracted from AgentDetail.vue so it can be spec-tested on its own: the
     parent needs a router, a session and half of frappe-ui to mount at all. -->
<template>
	<section>
		<div class="flex items-center gap-2">
			<div class="text-base font-medium text-ink-gray-9">Access</div>
			<span
				v-if="dirty"
				data-test="access-dirty"
				title="Unsaved changes"
				class="inline-flex items-center gap-1 text-sm text-ink-amber-2"
			>
				<span class="size-1.5 rounded-full bg-surface-amber-2"></span>
				Unsaved changes
			</span>
		</div>
		<div class="mt-2 text-sm text-ink-gray-5">
			Choose who may install and run this agent. Access is enforced server-side on every
			path. With no roles and no users selected, only administrators can use it.
		</div>

		<!-- roles -->
		<div class="mt-5 text-sm font-medium text-ink-gray-7">Roles</div>
		<div class="mt-2 flex flex-wrap gap-1.5">
			<div
				v-for="r in roleDraft"
				:key="r"
				data-test="role-chip"
				class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
			>
				<span class="truncate">{{ r }}</span>
				<Button
					variant="ghost"
					icon="x"
					class="!h-4 !w-4"
					:label="'Remove role ' + r"
					@click="removeRole(r)"
				/>
			</div>
			<span v-if="!roleDraft.length" class="text-sm text-ink-gray-4">No roles</span>
		</div>
		<div class="mt-2 w-72">
			<Autocomplete
				:options="roleOptions"
				:modelValue="null"
				placeholder="Add a role…"
				@update:modelValue="(opt) => opt && addRole(opt.value)"
			/>
		</div>

		<!-- users -->
		<div class="mt-5 text-sm font-medium text-ink-gray-7">People</div>
		<div class="mt-2 flex flex-wrap gap-1.5">
			<div
				v-for="u in userDraft"
				:key="u"
				data-test="user-chip"
				class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
			>
				<span class="truncate">{{ u }}</span>
				<Button
					variant="ghost"
					icon="x"
					class="!h-4 !w-4"
					:label="'Remove user ' + u"
					@click="removeUser(u)"
				/>
			</div>
			<span v-if="!userDraft.length" class="text-sm text-ink-gray-4">No people</span>
		</div>
		<div class="mt-2 w-72">
			<JvCombo
				:modelValue="userQuery"
				:options="userOptions"
				allow-custom
				placeholder="Search people…"
				@update:modelValue="onUserPick"
			/>
		</div>

		<!-- apply + save -->
		<label class="mt-5 flex max-w-md items-start gap-2 text-sm text-ink-gray-7">
			<input
				type="checkbox"
				data-test="apply-now"
				class="mt-0.5"
				:checked="applyNow"
				@change="applyNow = $event.target.checked"
			/>
			<span>
				Apply to workspace now (restarts your workspace for about 30 seconds; active chats
				are interrupted)
			</span>
		</label>

		<div class="mt-4 flex items-center gap-2">
			<Button
				data-test="save-access"
				label="Save access"
				variant="solid"
				:loading="saving"
				:disabled="!dirty || saving"
				@click="save"
			/>
			<Button v-if="dirty && !saving" label="Reset" variant="ghost" @click="reset" />
			<span
				v-if="pending"
				data-test="apply-pending"
				class="inline-flex h-5 items-center rounded-full bg-surface-amber-1 px-2 text-xs text-ink-amber-3"
			>
				Applying to workspace…
			</span>
		</div>
	</section>
</template>

<script setup>
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { Autocomplete, Button, toast } from "frappe-ui";
import JvCombo from "@/components/JvCombo.vue";
import * as api from "@/api";
import * as apiAgents from "@/api/agents";
import { errHtml } from "@/lib/errors";

const props = defineProps({
	slug: { type: String, required: true },
	roles: { type: Array, default: () => [] }, // saved allowed_roles
	users: { type: Array, default: () => [] }, // saved allowed_users
	allRoles: { type: Array, default: () => [] }, // selectable Role names
});
const emit = defineEmits(["saved"]);

// How long between polls while an apply is in flight. Matches AgentsList's apply
// pill so the two surfaces report the same restart at the same cadence.
const POLL_MS = 3000;

// The SAVED baseline, owned here rather than read straight off the props. The
// props are the parent's copy, and it only learns the new value from our own
// `saved` emit - deriving "dirty" from them would leave the editor showing
// unsaved changes after a save that demonstrably succeeded, until the parent got
// around to re-rendering. The server's response is the authority for what is now
// saved, so that is what the baseline becomes.
const savedRoles = ref([...props.roles]);
const savedUsers = ref([...props.users]);
const roleDraft = ref([...props.roles]);
const userDraft = ref([...props.users]);
const saving = ref(false);
// Defaults ON: an access change the admin cannot see take effect is the thing
// they came here to do, and leaving it unapplied is how a roster and a DB
// silently disagree until some unrelated mutation pushes.
const applyNow = ref(true);

// A new agent (or a reload of this one) reseeds both the baseline and the draft.
watch(
	() => [props.roles, props.users],
	() => {
		savedRoles.value = [...props.roles];
		savedUsers.value = [...props.users];
		roleDraft.value = [...props.roles];
		userDraft.value = [...props.users];
	}
);

// Order is not a change: the server returns its own order, and removing then
// re-adding the same role has changed nothing.
const sorted = (a) => [...a].sort().join("|");
const dirty = computed(
	() =>
		sorted(roleDraft.value) !== sorted(savedRoles.value) ||
		sorted(userDraft.value) !== sorted(savedUsers.value)
);

const roleOptions = computed(() => {
	const taken = new Set(roleDraft.value);
	return (props.allRoles || [])
		.filter((r) => !taken.has(r))
		.map((r) => ({ label: r, value: r }));
});
function addRole(r) {
	if (!roleDraft.value.includes(r)) roleDraft.value = [...roleDraft.value, r];
}
function removeRole(r) {
	roleDraft.value = roleDraft.value.filter((x) => x !== r);
}

// ── user type-ahead ──────────────────────────────────────────────────────────
// Remote, not a local filter over a preloaded directory: a tenant's user list is
// unbounded and search_users caps at 20 server-side.
const userQuery = ref("");
const userResults = ref([]);
let searchTimer = null;

const userOptions = computed(() => {
	const taken = new Set(userDraft.value);
	return userResults.value
		.filter((u) => !taken.has(u.name))
		.map((u) => ({
			value: u.name,
			label: u.full_name ? `${u.full_name} (${u.name})` : u.name,
		}));
});

async function runSearch(q) {
	try {
		userResults.value = (await apiAgents.searchUsers(q)) || [];
	} catch {
		// A failed lookup must not blank the picker mid-typing; the admin can still
		// finish typing an address they already know.
		userResults.value = [];
	}
}
function onUserPick(v) {
	const value = v || "";
	// JvCombo in allow-custom mode emits both keystrokes and the chosen option, so
	// a value matching a fetched user IS the selection.
	if (userResults.value.some((u) => u.name === value)) {
		if (!userDraft.value.includes(value)) userDraft.value = [...userDraft.value, value];
		userQuery.value = "";
		userResults.value = [];
		return;
	}
	userQuery.value = value;
	clearTimeout(searchTimer);
	searchTimer = setTimeout(() => runSearch(value), 200);
}
function removeUser(u) {
	userDraft.value = userDraft.value.filter((x) => x !== u);
}

// ── apply status ─────────────────────────────────────────────────────────────
const pending = ref(false);
let pollTimer = null;

function stopPoll() {
	clearTimeout(pollTimer);
	pollTimer = null;
}
onBeforeUnmount(() => {
	stopPoll();
	clearTimeout(searchTimer);
});

async function pollUntilTerminal() {
	let s;
	try {
		s = (await api.getAgentsSyncStatus()) || {};
	} catch (e) {
		pending.value = false;
		toast.error(errHtml(e));
		return;
	}
	if (s.pending) {
		pollTimer = setTimeout(pollUntilTerminal, POLL_MS);
		return;
	}
	pending.value = false;
	const status = String(s.last_sync_status || "");
	if (status.startsWith("failed")) {
		// The reason must survive as the message, not be flattened to "apply
		// failed" - it is the only thing that says WHY the workspace refused it.
		toast.error(status.replace(/^failed:\s*/, "") || "Apply failed.");
		return;
	}
	toast.success("Access saved and applied.");
}

async function save() {
	if (saving.value || !dirty.value) return;
	saving.value = true;
	const wantApply = applyNow.value;
	try {
		const res = await apiAgents.setAgentAccess(
			props.slug,
			roleDraft.value,
			userDraft.value,
			wantApply
		);
		const nextRoles = (res && res.allowed_roles) || [];
		const nextUsers = (res && res.allowed_users) || [];
		savedRoles.value = [...nextRoles];
		savedUsers.value = [...nextUsers];
		roleDraft.value = [...nextRoles];
		userDraft.value = [...nextUsers];
		emit("saved", { allowed_roles: nextRoles, allowed_users: nextUsers });
		if (res && res.applied) {
			pending.value = true;
			stopPoll();
			pollUntilTerminal();
		} else {
			toast.success("Access saved. Apply catalog changes to make it runnable.");
		}
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}

function reset() {
	roleDraft.value = [...savedRoles.value];
	userDraft.value = [...savedUsers.value];
}

defineExpose({ dirty, roleDraft, userDraft, applyNow, pending });
</script>
