<template>
	<!-- Picks who another user's skill goes to, then says what arrives and asks once.
	     A dialog over the Skills page's Admin tab, as SkillHoldDialog is. -->
	<Dialog v-model="show" :options="{ title }">
		<!-- Wraps anywhere; the rule is in SkillHoldDialog. -->
		<template #body-title>
			<h3 class="jv-skill-dialog-title text-2xl font-semibold leading-7 text-ink-gray-9">
				{{ title }}
			</h3>
		</template>
		<template #body-content>
			<div ref="bodyEl" class="flex flex-col gap-3">
				<p v-if="shared" class="jv-skill-handover-reviewer text-p-sm text-ink-gray-7">
					This skill is shared with a role or with everyone, so only a skill reviewer can
					receive it.
				</p>
				<FormControl
					type="text"
					label="New owner"
					placeholder="Search by name or email"
					:modelValue="search"
					:disabled="saving"
					@update:modelValue="onSearch"
				/>
				<div v-if="meAvailable" class="flex items-center gap-2">
					<Button
						class="jv-skill-handover-self"
						size="sm"
						label="Hand to myself"
						:disabled="saving"
						@click="chosen = me"
					/>
				</div>
				<p
					v-if="loadError"
					class="jv-skill-handover-load-error text-sm text-ink-red-4"
					role="alert"
				>
					{{ loadError }}
				</p>
				<p v-else-if="!loaded" class="text-sm text-ink-gray-6" role="status">Loading…</p>
				<p v-else-if="!targets.length" class="text-sm text-ink-gray-6" role="status">
					Nobody who can use Jarvis matches.
				</p>
				<p
					v-if="loaded && searching"
					class="jv-skill-handover-searching text-sm text-ink-gray-6"
					role="status"
				>
					Searching…
				</p>
				<!-- min-w-0: a fieldset is as wide as its longest line by default, and a
				     long name would push it past the dialog. -->
				<fieldset
					v-if="!loadError && loaded && targets.length"
					class="flex max-h-56 min-w-0 flex-col gap-1 overflow-y-auto"
					:disabled="saving"
					:aria-busy="searching ? 'true' : 'false'"
				>
					<legend class="sr-only">New owner</legend>
					<label
						v-for="(u, i) in targets"
						:key="u.user"
						class="jv-skill-handover-user flex items-center gap-2 rounded px-2 py-1 text-sm"
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
							name="jv-skill-handover-user"
							:value="u.user"
							:checked="!!chosen && chosen.user === u.user"
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
					class="jv-skill-handover-more text-xs text-ink-gray-6"
				>
					Showing the first {{ targets.length }}. Type a name to narrow the search.
				</p>
				<!-- The confirmation: who to whom is said once, in full; what happens
				     then is a short list that names nobody again. -->
				<div
					v-if="chosen"
					class="jv-skill-handover-summary flex min-w-0 flex-col gap-2 border-t pt-3"
				>
					<p class="break-words text-p-sm font-medium text-ink-gray-8">
						Hand “{{ label }}” from {{ ownerLabel || "its owner" }} to
						{{ userLabel(chosen) }}?
					</p>
					<ul class="list-disc space-y-1 pl-5 text-p-sm text-ink-gray-7">
						<li>It arrives switched off.</li>
						<li>Its shares are removed.</li>
						<li>It no longer writes without asking.</li>
						<li>Its File Box opt-in is switched off.</li>
						<li>Its assignments are cleared.</li>
						<li>Any hold on it is lifted.</li>
					</ul>
				</div>
				<ErrorMessage v-if="error" :message="error" />
				<div v-if="changed" class="flex">
					<Button
						class="jv-skill-handover-reload"
						size="sm"
						label="Reload"
						@click="reload"
					/>
				</div>
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-skill-handover-confirm"
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
// refuses a skill that changed hands since). The tab re-reads its list when told
// (`handed`, and `failed`: it passes back the row as re-read, or closes this when
// the skill is no longer listed). A refusal stays in the dialog, next to the choice
// it is about; the one that says the owner changed also offers Reload, which asks
// the tab for the row again. While the hand over is in flight the dialog stays
// open: its answer belongs here.
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, FormControl, toast } from "frappe-ui";
import { adminHandover, adminHandoverTargets } from "@/api/skillsAdmin";
import { session } from "@/data/session";
import { errMessage, escapeHtml } from "@/lib/errors";

// The same pause as the tab's own search.
const SEARCH_WAIT_MS = 300;
// What the server's refusal for a skill that changed hands says ("changed hands since
// the list was loaded. Reload and try again.").
const CHANGED = /\b(changed|reload)\b/i;
// The framework's answer when the method does not exist: the server runs a release
// from before skill hand overs (this SPA is newer than the app on the site).
const NOT_UPDATED =
	"This site has not been updated for handing skills over yet. Ask whoever looks after it to update Jarvis.";
const APPLY_HINT = "Press Apply to update the shared assistant.";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The skill's row name, its own name, its owner as loaded, and how to show the owner.
	name: { type: String, required: true },
	skillName: { type: String, default: "" },
	owner: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
	// A role or company skill: only a skill reviewer can receive it.
	shared: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "handed", "failed", "reload"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => {
		if (!v && saving.value) return; // Escape or a click outside, mid hand over
		emit("update:modelValue", v);
	},
});
const search = ref("");
const targets = ref([]);
const loaded = ref(false);
const searching = ref(false);
const more = ref(false);
const loadError = ref("");
// The person picked (the whole target, so a pick the list no longer shows still reads).
const chosen = ref(null);
// The signed-in user as a target, once the server has listed them.
const me = ref(null);
const saving = ref(false);
const error = ref("");
// The refusal in `error` is the one for a changed owner: Reload is offered.
const changed = ref(false);
const bodyEl = ref(null);

const label = computed(() => props.skillName || props.name);
const title = computed(() => `Hand over “${label.value}”`);
const meAvailable = computed(() => !!me.value && !unavailable(me.value));
const canSave = computed(() => !saving.value && !!chosen.value && !unavailable(chosen.value));

function userLabel(u) {
	return u.full_name && u.full_name !== u.user ? `${u.full_name} (${u.user})` : u.user;
}
// Why a user cannot be picked, or "" when they can. The server checks again.
function unavailable(u) {
	if (u.user === props.owner) return "is already its owner";
	if (u.at_cap) return "has 25 skills already";
	return "";
}
// Unique per dialog, for the reasons' ids.
const idBase = `jv-skill-handover-${Math.random().toString(36).slice(2, 10)}`;
function reasonId(i) {
	return `${idBase}-why-${i}`;
}
// A radio can be checked by a click or by the arrow keys: on an unavailable person
// the group is put back as it was (checking one radio unchecked the picked one).
function onPick(event, u) {
	if (!unavailable(u)) {
		chosen.value = u;
		return;
	}
	event.preventDefault();
	const group = event.target.closest("fieldset") || event.target.parentNode;
	for (const r of group.querySelectorAll("input[type=radio]"))
		r.checked = !!chosen.value && r.value === chosen.value.user;
}

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
		const res = (await adminHandoverTargets(search.value.trim(), props.name)) || {};
		if (mine !== latest) return;
		targets.value = Array.isArray(res.targets) ? res.targets : [];
		more.value = !!res.more;
		loadError.value = "";
		me.value = targets.value.find((u) => u.user === session.user) || me.value;
		// A choice the new results no longer show is no choice, except the shortcut's.
		const kept = chosen.value && (chosen.value === me.value || listed(chosen.value.user));
		if (!kept) chosen.value = null;
	} catch (e) {
		if (mine !== latest) return;
		targets.value = [];
		more.value = false;
		chosen.value = null;
		loadError.value = errorText(e, "Could not load the users.");
	} finally {
		if (mine === latest) {
			loaded.value = true;
			searching.value = false;
		}
	}
}
const listed = (user) => targets.value.some((u) => u.user === user);

// The first page may not hold the signed-in user: ask for them by address.
async function findMe() {
	if (!session.user || me.value) return;
	try {
		const res = (await adminHandoverTargets(session.user, props.name)) || {};
		me.value = (res.targets || []).find((u) => u.user === session.user) || null;
	} catch (e) {
		// no shortcut; the picker still works
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
	async (open) => {
		if (!open) return;
		search.value = "";
		chosen.value = null;
		me.value = null;
		error.value = "";
		changed.value = false;
		loaded.value = false;
		// The dialog puts focus on its close button: typing a name would press it.
		nextTick(() =>
			requestAnimationFrame(() => {
				const field = bodyEl.value && bodyEl.value.querySelector("input[type=text]");
				if (field) field.focus();
			})
		);
		await load();
		if (more.value) findMe();
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	clearTimeout(searchTimer);
	latest++;
});

function reload() {
	error.value = "";
	changed.value = false;
	emit("reload", { name: props.name });
}

async function save() {
	if (!canSave.value) return;
	const to = chosen.value;
	saving.value = true;
	error.value = "";
	changed.value = false;
	try {
		const res = (await adminHandover(props.name, to.user, props.owner)) || {};
		// A toast renders HTML: every name goes in escaped.
		toast.success(
			`Handed “${escapeHtml(label.value)}” to ${escapeHtml(userLabel(to))}.` +
				(res.needs_apply ? ` ${APPLY_HINT}` : "")
		);
		emit("handed", {
			name: props.name,
			new_owner: res.new_owner || to.user,
			needs_apply: !!res.needs_apply,
		});
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errorText(e, "Could not hand the skill over.");
		changed.value = CHANGED.test(error.value);
		emit("failed", { name: props.name, message: error.value, changed: changed.value });
	} finally {
		saving.value = false;
	}
}
</script>
