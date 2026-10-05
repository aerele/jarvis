<template>
	<!-- Picks who another user's macro goes to, then says what arrives and asks
	     once. A second dialog over Settings, as MacroHoldDialog is, so Escape
	     closes this one alone. -->
	<Dialog v-model="show" :options="{ title }">
		<template #body-content>
			<div class="flex flex-col gap-3">
				<FormControl
					type="text"
					label="New owner"
					placeholder="Search by name or email"
					:modelValue="search"
					:disabled="saving"
					@update:modelValue="onSearch"
				/>
				<p
					v-if="loadError"
					class="jv-macro-handover-load-error text-sm text-ink-red-4"
					role="alert"
				>
					{{ loadError }}
				</p>
				<p v-else-if="!loaded" class="text-sm text-ink-gray-6" role="status">Loading…</p>
				<p v-else-if="!users.length" class="text-sm text-ink-gray-6" role="status">
					Nobody who can use Jarvis macros matches.
				</p>
				<p
					v-if="loaded && searching"
					class="jv-macro-handover-searching text-sm text-ink-gray-6"
					role="status"
				>
					Searching…
				</p>
				<fieldset
					v-if="!loadError && loaded && users.length"
					class="flex max-h-56 flex-col gap-1 overflow-y-auto"
					:disabled="saving"
					:aria-busy="searching ? 'true' : 'false'"
				>
					<legend class="sr-only">New owner</legend>
					<label
						v-for="(u, i) in users"
						:key="u.user"
						class="jv-macro-handover-user flex items-center gap-2 rounded px-2 py-1 text-sm"
						:class="
							unavailable(u)
								? 'text-ink-gray-5'
								: 'text-ink-gray-8 hover:bg-surface-gray-2'
						"
						:data-user="u.user"
					>
						<!-- An unavailable person is aria-disabled, not disabled: a keyboard
						     or screen reader user still reaches them and hears why. -->
						<input
							type="radio"
							name="jv-macro-handover-user"
							:value="u.user"
							:checked="picked === u.user"
							:aria-disabled="unavailable(u) ? 'true' : undefined"
							:aria-describedby="unavailable(u) ? reasonId(i) : undefined"
							@click="onPick($event, u)"
							@change="onPick($event, u)"
						/>
						<span class="min-w-0 flex-1 truncate">{{ userLabel(u) }}</span>
						<span v-if="unavailable(u)" :id="reasonId(i)" class="text-xs">{{
							unavailable(u)
						}}</span>
					</label>
				</fieldset>
				<p
					v-if="!loadError && loaded && more"
					class="jv-macro-handover-more text-xs text-ink-gray-6"
				>
					Showing the first {{ users.length }}. Type a name to narrow the search.
				</p>
				<!-- The confirmation: what the admin is about to do, in words. -->
				<p v-if="pickedUser" class="jv-macro-handover-summary text-p-sm text-ink-gray-7">
					Hand “{{ macroLabel }}” from {{ ownerLabel || "its owner" }} to
					{{ userLabel(pickedUser) }}? It arrives switched off, unscheduled and not
					armed. Its summary, the skills on its steps, its shares and its assignments are
					cleared, and a run that is going now is stopped. Both of them are told. Its
					past runs stay with {{ ownerLabel || "its owner" }}.
				</p>
				<ErrorMessage v-if="error" :message="error" />
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-macro-handover-confirm"
					variant="solid"
					label="Hand over"
					:loading="saving"
					:disabled="!canSave"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// Search, pick, then admin_handover, with the owner this dialog showed (the server
// refuses a macro that changed hands since). The pane re-reads its list when told
// (`handed`, and `failed`: a hand-over can stop at a run that would not stop and
// leave the macro on hold, which the list should show; the pane then passes the
// owner it re-read, so a retry is not refused as "changed hands" again, or closes
// this with the message when the macro is no longer listed). A refusal stays in
// the dialog, next to the choice it is about. While the hand-over is in flight the
// dialog stays open: its answer belongs here.
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, FormControl, toast } from "frappe-ui";
import { adminHandover, adminHandoverTargets } from "@/api/macrosAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

const SEARCH_WAIT_MS = 300;

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The macro's row name, its own name, its owner, and how to show the owner.
	name: { type: String, required: true },
	macroName: { type: String, default: "" },
	owner: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "handed", "failed"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => {
		if (!v && saving.value) return; // Escape or a click outside, mid hand-over
		emit("update:modelValue", v);
	},
});
const search = ref("");
const users = ref([]);
const max = ref(0);
const loaded = ref(false);
const searching = ref(false);
const more = ref(false);
const loadError = ref("");
const picked = ref("");
const saving = ref(false);
const error = ref("");

const macroLabel = computed(() => props.macroName || props.name);
const title = computed(() => `Hand over “${macroLabel.value}”`);
const pickedUser = computed(() => users.value.find((u) => u.user === picked.value) || null);
const canSave = computed(
	() => !saving.value && !!pickedUser.value && !unavailable(pickedUser.value)
);

function userLabel(u) {
	return u.full_name && u.full_name !== u.user ? `${u.full_name} (${u.user})` : u.user;
}
// Why a user cannot be picked, or "" when they can. The server checks again.
function unavailable(u) {
	if (u.user === props.owner) return "is already its owner";
	if (max.value && Number(u.macros) >= max.value) return `has ${u.macros} macros, the most`;
	return "";
}
// Unique per dialog, for the reasons' ids.
const idBase = `jv-macro-handover-${Math.random().toString(36).slice(2, 10)}`;
function reasonId(i) {
	return `${idBase}-why-${i}`;
}
// A radio can be checked by a click or by the arrow keys: on an unavailable person
// the group is put back as it was (checking one radio unchecked the picked one).
function onPick(event, u) {
	if (!unavailable(u)) {
		picked.value = u.user;
		return;
	}
	event.preventDefault();
	const group = event.target.closest("fieldset") || event.target.parentNode;
	for (const r of group.querySelectorAll("input[type=radio]"))
		r.checked = r.value === picked.value;
}

// The framework's answer when the method does not exist: the server runs a release
// from before hand-overs (this SPA is newer than the app on the site).
const NOT_UPDATED =
	"This site has not been updated for handing macros over yet. Ask whoever looks after it to update Jarvis.";
function errorText(e, fallback) {
	const text = errMessage(e, fallback);
	return /^Failed to get method for command /.test(text) ? NOT_UPDATED : text;
}

// Only the newest search may fill the list: a slow answer to an earlier one must
// not replace the results of what was typed after it.
let latest = 0;
async function load() {
	const mine = ++latest;
	searching.value = true;
	try {
		const res = (await adminHandoverTargets(search.value.trim())) || {};
		if (mine !== latest) return;
		users.value = Array.isArray(res.users) ? res.users : [];
		max.value = Number(res.max) || 0;
		more.value = !!res.more;
		loadError.value = "";
		// A choice the new results no longer show is no choice.
		if (!users.value.some((u) => u.user === picked.value)) picked.value = "";
	} catch (e) {
		if (mine !== latest) return;
		users.value = [];
		more.value = false;
		picked.value = "";
		loadError.value = errorText(e, "Could not load the users.");
	} finally {
		if (mine === latest) {
			loaded.value = true;
			searching.value = false;
		}
	}
}

let searchTimer = null;
function onSearch(value) {
	search.value = value == null ? "" : String(value);
	clearTimeout(searchTimer);
	searchTimer = setTimeout(load, SEARCH_WAIT_MS);
}

watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		search.value = "";
		picked.value = "";
		error.value = "";
		loaded.value = false;
		load();
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	clearTimeout(searchTimer);
	latest++;
});

async function save() {
	if (!canSave.value) return;
	const to = pickedUser.value;
	saving.value = true;
	error.value = "";
	try {
		const res = (await adminHandover(props.name, to.user, props.owner)) || {};
		const stopped = Number(res.stopped_runs) || 0;
		// A toast renders HTML: every name goes in escaped.
		toast.success(
			`Handed “${escapeHtml(macroLabel.value)}” to ${escapeHtml(userLabel(to))}` +
				(stopped ? `; ${stopped} run${stopped === 1 ? "" : "s"} stopped` : "")
		);
		emit("handed", { name: props.name, new_owner: res.new_owner || to.user });
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errorText(e, "Could not hand the macro over.");
		emit("failed", { name: props.name, message: error.value });
	} finally {
		saving.value = false;
	}
}
</script>
