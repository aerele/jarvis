<template>
	<!-- Picks who another user's skill goes to (one person or several), then says what
	     arrives and asks once. A dialog over the Skills page's Admin tab, as
	     SkillHoldDialog is. -->
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
					receive it. A copy is a private skill, so anyone can have one.
				</p>
				<!-- The people picked sit in the search field as chips, so the field reads as
				     the answer to "New owners"; the list below picks and unpicks them. -->
				<div>
					<label :for="searchId" class="mb-1.5 block text-xs text-ink-gray-5"
						>New owners</label
					>
					<div
						class="jv-skill-handover-field flex min-h-8 min-w-0 flex-wrap items-center gap-1 rounded bg-surface-gray-2 px-1.5 py-1 focus-within:bg-surface-white focus-within:ring-2 focus-within:ring-outline-gray-3"
					>
						<span
							v-for="u in picked"
							:key="u.user"
							class="jv-skill-handover-chip inline-flex min-w-0 max-w-[14rem] items-center gap-1.5 rounded-full border border-outline-gray-2 bg-surface-white py-0.5 pl-0.5 pr-1 text-sm text-ink-gray-8"
							:data-user="u.user"
						>
							<span
								aria-hidden="true"
								class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-surface-gray-3 text-[10px] font-semibold text-ink-gray-7"
								>{{ initials(u) }}</span
							>
							<span class="truncate">{{ u.full_name || u.user }}</span>
							<button
								type="button"
								class="jv-skill-handover-unpick flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-ink-gray-5 hover:bg-surface-gray-3 hover:text-ink-gray-8"
								:aria-label="`Remove ${userLabel(u)}`"
								:disabled="saving"
								@click="unpick(u)"
							>
								<svg
									width="12"
									height="12"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="2.4"
									aria-hidden="true"
								>
									<path d="M18 6 6 18M6 6l12 12" />
								</svg>
							</button>
						</span>
						<input
							:id="searchId"
							type="text"
							class="jv-skill-handover-search h-7 min-w-[8rem] flex-1 border-0 bg-transparent px-1 text-base text-ink-gray-8 placeholder-ink-gray-4 focus:outline-none focus:ring-0"
							autocomplete="off"
							:placeholder="
								picked.length ? 'Add someone else' : 'Search by name or email'
							"
							:value="search"
							:disabled="saving"
							@input="onSearch($event.target.value)"
						/>
					</div>
				</div>
				<div v-if="meAvailable" class="flex items-center gap-2">
					<Button
						class="jv-skill-handover-self"
						size="sm"
						label="Hand to myself"
						:disabled="saving || isPicked(me) || atMax"
						@click="add(me)"
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
					class="flex max-h-56 min-w-0 flex-col gap-0.5 overflow-y-auto rounded border border-outline-gray-1 p-1"
					:disabled="saving"
					:aria-busy="searching ? 'true' : 'false'"
				>
					<legend class="sr-only">New owners</legend>
					<label
						v-for="(u, i) in targets"
						:key="u.user"
						class="jv-skill-handover-user flex items-center gap-2 rounded px-2 py-1 text-sm"
						:class="
							unavailable(u)
								? 'text-ink-gray-5'
								: 'cursor-pointer text-ink-gray-8 hover:bg-surface-gray-2'
						"
						:data-user="u.user"
					>
						<!-- An unavailable person is aria-disabled, not disabled: a keyboard
						     or screen reader user still reaches them and hears why. -->
						<input
							type="checkbox"
							:value="u.user"
							:checked="isPicked(u)"
							:aria-disabled="unavailable(u) ? 'true' : undefined"
							:aria-describedby="unavailable(u) ? reasonId(i) : undefined"
							@click="onPick($event, u)"
							@change="onPick($event, u)"
						/>
						<span
							aria-hidden="true"
							class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-gray-3 text-[10px] font-semibold text-ink-gray-7"
							>{{ initials(u) }}</span
						>
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
				<div
					v-if="picked.length"
					class="jv-skill-handover-count flex items-center justify-between text-xs text-ink-gray-6"
				>
					<span role="status">
						{{ picked.length }} selected<template v-if="atMax">
							· {{ MAX_PEOPLE }} is the most at once</template
						>
					</span>
					<button
						type="button"
						class="jv-skill-handover-clear text-ink-gray-8 underline underline-offset-2"
						:disabled="saving"
						@click="picked = []"
					>
						Clear
					</button>
				</div>
				<!-- The confirmation: who to whom is said once, in full; what happens
				     then is a short list that names nobody again. -->
				<div
					v-if="picked.length"
					class="jv-skill-handover-summary flex min-w-0 flex-col gap-2 border-t pt-3"
				>
					<p class="break-words text-p-sm font-medium text-ink-gray-8">
						{{ question }}
					</p>
					<ul class="list-disc space-y-1 pl-5 text-p-sm text-ink-gray-7">
						<li v-for="point in points" :key="point">{{ point }}</li>
					</ul>
					<label
						class="jv-skill-handover-keep flex cursor-pointer items-start gap-2 text-p-sm text-ink-gray-8"
					>
						<input v-model="keep" type="checkbox" class="mt-0.5" :disabled="saving" />
						<span
							>Keep it with {{ ownerName }} too
							<span class="block text-ink-gray-6"
								>Gives everyone a copy instead of moving it.</span
							></span
						>
					</label>
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
					:label="saveLabel"
					:loading="saving"
					:disabled="!canSave"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// Search, pick one or more, then admin_handover, with the owner this dialog showed
// (the server refuses a skill that changed hands since). The first person picked is
// handed the skill and each of the others gets a private copy; with "Keep" the skill
// stays and everyone picked gets a copy. The tab re-reads its list when told
// (`handed`, and `failed`: it passes back the row as re-read, or closes this when the
// skill is no longer listed). A refusal stays in the dialog, next to the choice it is
// about; the one that says the owner changed also offers Reload, which asks the tab
// for the row again. While the hand over is in flight the dialog stays open: its
// answer belongs here.
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, toast } from "frappe-ui";
import { adminHandover, adminHandoverTargets } from "@/api/skillsAdmin";
import { session } from "@/data/session";
import { errMessage, escapeHtml } from "@/lib/errors";

// The same pause as the tab's own search.
const SEARCH_WAIT_MS = 300;
// How many people one hand over may name; the server holds the same limit.
const MAX_PEOPLE = 10;
// The server's refusal for a skill that changed hands, by its exception class: the
// message is free text (a skill may be named "changed"), so it is never read for this.
const OWNER_CHANGED = "SkillOwnerChangedError";
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
// The people picked, in the order picked, as whole targets: a pick stays when a new
// search no longer lists it, so several can be gathered from several searches.
const picked = ref([]);
const keep = ref(false);
// The signed-in user as a target, once the server has listed them.
const me = ref(null);
// How many skills one person may own, as the server says (null until it does).
const cap = ref(null);
const saving = ref(false);
const error = ref("");
// The refusal in `error` is the one for a changed owner: Reload is offered.
const changed = ref(false);
const bodyEl = ref(null);

const label = computed(() => props.skillName || props.name);
const title = computed(() => `Hand over “${label.value}”`);
const ownerName = computed(() => props.ownerLabel || "its owner");
const meAvailable = computed(() => !!me.value && !unavailable(me.value));
const atMax = computed(() => picked.value.length >= MAX_PEOPLE);
const canSave = computed(
	() => !saving.value && picked.value.length > 0 && !picked.value.some(unavailable)
);
const many = computed(() => picked.value.length > 1);
const question = computed(() => {
	const to = many.value
		? listed(picked.value.map((u) => u.full_name || u.user))
		: userLabel(picked.value[0]);
	return keep.value
		? `Give ${to} a copy of “${label.value}” from ${ownerName.value}?`
		: `Hand “${label.value}” from ${ownerName.value} to ${to}?`;
});
const points = computed(() => {
	if (keep.value)
		return [
			`${
				many.value ? "Each of them gets" : "They get"
			} their own private copy; the original stays as it is.`,
			"A copy arrives switched off and asks before every write.",
			"A copy has no shares, no assignments and no File Box opt-in.",
			"Everyone is told.",
		];
	if (many.value)
		return [
			"The first person picked gets it; each of the others gets their own private copy.",
			"It and the copies arrive switched off.",
			"Its shares are removed.",
			"It no longer writes without asking.",
			"Its File Box opt-in is switched off.",
			"Its assignments are cleared.",
			"Any hold on it is lifted.",
			"Everyone is told.",
		];
	return [
		"It arrives switched off.",
		"Its shares are removed.",
		"It no longer writes without asking.",
		"Its File Box opt-in is switched off.",
		"Its assignments are cleared.",
		"Any hold on it is lifted.",
	];
});
const saveLabel = computed(() => {
	if (many.value) return `Hand over to ${picked.value.length} people`;
	return keep.value ? "Give a copy" : "Hand over";
});

function userLabel(u) {
	return u.full_name && u.full_name !== u.user ? `${u.full_name} (${u.user})` : u.user;
}
function initials(u) {
	const words = String(u.full_name || u.user)
		.split(/[\s@.]+/)
		.filter(Boolean);
	return words
		.slice(0, 2)
		.map((w) => w[0])
		.join("")
		.toUpperCase();
}
function listed(names) {
	return names.length < 2
		? names.join("")
		: `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
// Why a user cannot be picked, or "" when they can. The server checks again.
function unavailable(u) {
	if (u.user === props.owner) return "is already its owner";
	if (u.at_cap)
		return cap.value
			? `has ${cap.value} skills already`
			: "has the most skills one person may";
	return "";
}
const isPicked = (u) => !!u && picked.value.some((p) => p.user === u.user);
function add(u) {
	if (!u || isPicked(u) || atMax.value || unavailable(u)) return;
	picked.value = [...picked.value, u];
}
function unpick(u) {
	picked.value = picked.value.filter((p) => p.user !== u.user);
}
// Unique per dialog, for the reasons' and the field's ids.
const idBase = `jv-skill-handover-${Math.random().toString(36).slice(2, 10)}`;
const searchId = `${idBase}-search`;
function reasonId(i) {
	return `${idBase}-why-${i}`;
}
// A box can be ticked by a click or by the space bar. An unavailable person, or one
// more than the most, is put back as it was: a click is cancelled (the browser
// restores the box itself), a change that got through is undone by hand.
function onPick(event, u) {
	const blocked = unavailable(u) || (!isPicked(u) && atMax.value);
	if (blocked) {
		if (event.type === "click") event.preventDefault();
		else event.target.checked = isPicked(u);
		return;
	}
	if (event.type !== "change") return;
	if (event.target.checked) add(u);
	else unpick(u);
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
		cap.value = Number.isInteger(res.cap) ? res.cap : cap.value;
		loadError.value = "";
		me.value = targets.value.find((u) => u.user === session.user) || me.value;
		// A pick the new results show is refreshed from them (it may have reached the
		// cap); one they do not show is kept as it was.
		picked.value = picked.value.map((p) => targets.value.find((u) => u.user === p.user) || p);
	} catch (e) {
		if (mine !== latest) return;
		targets.value = [];
		more.value = false;
		loadError.value = errorText(e, "Could not load the users.");
	} finally {
		if (mine === latest) {
			loaded.value = true;
			searching.value = false;
		}
	}
}

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
		picked.value = [];
		keep.value = false;
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

function byUser(user) {
	const u = picked.value.find((p) => p.user === user);
	return u ? userLabel(u) : user;
}

async function save() {
	if (!canSave.value) return;
	const to = picked.value.slice();
	const keeping = keep.value;
	saving.value = true;
	error.value = "";
	changed.value = false;
	try {
		const res =
			(await adminHandover(
				props.name,
				to.length === 1 ? to[0].user : to.map((u) => u.user),
				props.owner,
				keeping
			)) || {};
		const copies = Array.isArray(res.copies) ? res.copies : [];
		const failed = Array.isArray(res.copy_failed) ? res.copy_failed : [];
		// A toast renders HTML: every name goes in escaped.
		const name = escapeHtml(label.value);
		const parts = [];
		if (res.handed_over !== false && !keeping)
			parts.push(`Handed “${name}” to ${escapeHtml(byUser(res.new_owner || to[0].user))}`);
		if (copies.length)
			parts.push(
				`${parts.length ? "copies went to" : `Gave a copy of “${name}” to`} ` +
					escapeHtml(listed(copies.map((c) => byUser(c.user))))
			);
		if (parts.length)
			toast.success(`${parts.join("; ")}.` + (res.needs_apply ? ` ${APPLY_HINT}` : ""));
		if (failed.length)
			toast.error(
				escapeHtml(
					`Not copied for ${failed
						.map((f) => `${byUser(f.user)}: ${f.reason}`)
						.join(" ")}`
				)
			);
		emit("handed", {
			name: props.name,
			new_owner: keeping ? null : res.new_owner || to[0].user,
			needs_apply: !!res.needs_apply,
			copies,
		});
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errorText(e, "Could not hand the skill over.");
		changed.value = !!e && e.exc_type === OWNER_CHANGED;
		emit("failed", { name: props.name, message: error.value, changed: changed.value });
	} finally {
		saving.value = false;
	}
}
</script>
