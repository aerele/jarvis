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
		<!-- The SAME frappe-ui Autocomplete as the roles picker above, so the two
		     halves of one statement look and behave alike; its popover is portaled
		     (reka PopoverPortal) with a solid bg-surface-modal, so it cannot be
		     clipped by this section's grid track or read through to the copy below.
		     Remote-backed: @update:query drives a debounced search_users call and
		     :options is re-fed from the response. focusin (which bubbles, unlike
		     focus) and click prime it with the empty query on open, so the first 20
		     people are there instead of an empty box that only fills once you guess
		     a letter. -->
		<div class="mt-2 w-72" @focusin="primeUserMenu" @click="primeUserMenu">
			<Autocomplete
				:options="userOptions"
				:modelValue="null"
				placeholder="Add a person…"
				@update:query="onUserQuery"
				@update:modelValue="(opt) => opt && addUser(opt.value)"
			/>
		</div>

		<!-- Saving always applies, so the cost is stated as a fact rather than
		     offered as a choice: an access change that is saved but not loaded is a
		     half-done action, and the roster and the database silently disagreeing
		     is the #457 class of bug this feature keeps having to design around. -->
		<p data-test="apply-note" class="mt-5 max-w-md text-sm text-ink-gray-5">
			Saving loads this agent on your workspace. If the set of loaded agents changes, the
			workspace restarts for about 30 seconds and active chats are interrupted.
		</p>

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
import * as api from "@/api";
import * as apiAgents from "@/api/agents";
import { errHtml } from "@/lib/errors";
import { humaniseSyncStatus } from "@/lib/syncStatus";

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

// A props refresh always updates the BASELINE - that is the server's current
// truth - but only reseeds the drafts when there is nothing to lose. The parent
// reloads the agent for unrelated reasons (toggling Enabled calls load() and
// reassigns `agent`), and blowing away a half-finished access edit because of a
// switch somewhere else on the page is silent data loss. When the drafts are
// kept, `dirty` is recomputed against the new baseline, so the indicator stays
// honest: an edit that now matches what the server holds reads as clean.
watch(
	() => [props.roles, props.users],
	() => {
		const wasDirty = dirty.value;
		savedRoles.value = [...props.roles];
		savedUsers.value = [...props.users];
		if (!wasDirty) {
			roleDraft.value = [...props.roles];
			userDraft.value = [...props.users];
		}
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
// Monotonic request id: a slower earlier lookup must not overwrite a newer one.
// Typing "an" then "ann" and having "an" land second would repopulate the menu
// with results for a prefix the admin has already moved past.
let searchSeq = 0;
// Whether the menu has been filled for the CURRENT query. Reset whenever the
// results are cleared, so reopening after a pick fetches again.
const userMenuPrimed = ref(false);

function primeUserMenu() {
	if (userMenuPrimed.value) return;
	userMenuPrimed.value = true;
	// The current query, which is usually empty - search_users answers an empty
	// term with the first 20 enabled users, which is exactly the "who is there?"
	// an admin opening the picker is asking.
	runSearch(userQuery.value);
}

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
	const seq = ++searchSeq;
	try {
		const rows = (await apiAgents.searchUsers(q)) || [];
		if (seq === searchSeq) userResults.value = rows;
	} catch {
		// A failed lookup must not blank the picker mid-typing; the admin can still
		// finish typing an address they already know. Still ordered: a stale error
		// must not clear results a newer request has already delivered.
		if (seq === searchSeq) userResults.value = [];
	}
}
// Autocomplete owns its own search box and reports what was typed; selection is a
// separate event, so unlike the previous free-text combobox there is no need to
// guess whether a value is a keystroke or a pick.
function onUserQuery(q) {
	userQuery.value = q || "";
	userMenuPrimed.value = true; // typing owns the menu from here
	clearTimeout(searchTimer);
	searchTimer = setTimeout(() => runSearch(userQuery.value), 200);
}

function addUser(value) {
	if (!value) return;
	if (!userDraft.value.includes(value)) userDraft.value = [...userDraft.value, value];
	// Selected people drop out of userOptions, so refetch on the next open rather
	// than showing a menu one entry short of what the server would return.
	userQuery.value = "";
	userResults.value = [];
	userMenuPrimed.value = false;
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
	// Classified by the shared humaniser, not by a local "failed:" prefix strip, so
	// this toast cannot drift from the apply pill on the catalog page - the two
	// report the same pipeline and used to parse its status two different ways.
	const human = humaniseSyncStatus(s.last_sync_status);
	if (human.kind === "failed") {
		// The DETAIL is the only thing that says WHY the workspace refused it, so it
		// has to survive rather than being flattened to "apply failed".
		toast.error(
			human.detail ? `Access saved, but applying it failed - ${human.detail}` : human.text
		);
		return;
	}
	toast.success("Access saved and applied.");
}

async function save() {
	if (saving.value || !dirty.value) return;
	saving.value = true;
	try {
		// Always applies. The endpoint keeps its `apply` parameter defaulting to
		// false for API compatibility, so this passes it explicitly.
		const res = await apiAgents.setAgentAccess(
			props.slug,
			roleDraft.value,
			userDraft.value,
			true
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
			// Defensive: with apply always requested the server should report
			// applied. If it ever declines, say so rather than implying it is live.
			toast.success("Access saved, but not loaded yet. Use Apply catalog changes.");
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

defineExpose({
	dirty,
	roleDraft,
	userDraft,
	pending,
	userOptions,
	primeUserMenu,
	onUserQuery,
	addUser,
});
</script>
