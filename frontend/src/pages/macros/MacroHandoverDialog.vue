<template>
	<!-- Picks who another user's macro goes to (one person or several), then says what
	     arrives and asks once. A dialog over the Macros page's Admin tab, as
	     MacroHoldDialog is. -->
	<Dialog v-model="show" :options="{ title }">
		<!-- Wraps anywhere, as the hold dialog's title does (the rule is in its file). -->
		<template #body-title>
			<h3 class="jv-macro-dialog-title text-2xl font-semibold leading-7 text-ink-gray-9">
				{{ title }}
			</h3>
		</template>
		<template #body-content>
			<div ref="bodyEl" class="flex flex-col gap-3">
				<!-- The people picked sit in the search field as chips, so the field reads as
				     the answer to "New owners"; the list below picks and unpicks them. -->
				<div>
					<label :for="searchId" class="mb-1.5 block text-xs text-ink-gray-5"
						>New owners</label
					>
					<div
						class="jv-macro-handover-field flex min-h-8 min-w-0 flex-wrap items-center gap-1 rounded bg-surface-gray-2 px-1.5 py-1 focus-within:bg-surface-white focus-within:ring-2 focus-within:ring-outline-gray-3"
					>
						<span
							v-for="u in picked"
							:key="u.user"
							class="jv-macro-handover-chip inline-flex min-w-0 max-w-[14rem] items-center gap-1.5 rounded-full border border-outline-gray-2 bg-surface-white py-0.5 pl-0.5 pr-1 text-sm text-ink-gray-8"
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
								class="jv-macro-handover-unpick flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-ink-gray-5 hover:bg-surface-gray-3 hover:text-ink-gray-8"
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
							class="jv-macro-handover-search h-7 min-w-[8rem] flex-1 border-0 bg-transparent px-1 text-base text-ink-gray-8 placeholder-ink-gray-4 focus:outline-none focus:ring-0"
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
				<!-- min-w-0: a fieldset is as wide as its longest line by default, and a
				     long name would push it past the dialog. -->
				<fieldset
					v-if="!loadError && loaded && users.length"
					class="flex max-h-56 min-w-0 flex-col gap-0.5 overflow-y-auto rounded border border-outline-gray-1 p-1"
					:disabled="saving"
					:aria-busy="searching ? 'true' : 'false'"
				>
					<legend class="sr-only">New owners</legend>
					<label
						v-for="(u, i) in users"
						:key="u.user"
						class="jv-macro-handover-user flex items-center gap-2 rounded px-2 py-1 text-sm"
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
							class="h-3.5 w-3.5 shrink-0 rounded-sm border-outline-gray-4 bg-surface-white text-ink-gray-9 transition hover:border-outline-gray-5 focus:ring-0 focus:ring-offset-0 focus-visible:ring-2 focus-visible:ring-outline-gray-3"
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
					class="jv-macro-handover-more text-xs text-ink-gray-6"
				>
					Showing the first {{ users.length }}. Type a name to narrow the search.
				</p>
				<div
					v-if="picked.length"
					class="jv-macro-handover-count flex items-center justify-between text-xs text-ink-gray-6"
				>
					<span role="status">
						{{ picked.length }} selected<template v-if="atMax">
							· {{ MAX_PEOPLE }} is the most at once</template
						>
					</span>
					<button
						type="button"
						class="jv-macro-handover-clear text-ink-gray-8 underline underline-offset-2"
						:disabled="saving"
						@click="picked = []"
					>
						Clear
					</button>
				</div>
				<!-- The confirmation: what the admin is about to do, in words. Who to
				     whom is said once, in full; what happens then is a short list that
				     names nobody again, so it reads at a glance however long the names
				     are. Set apart from the choice above by a rule. -->
				<div
					v-if="picked.length"
					class="jv-macro-handover-summary flex min-w-0 flex-col gap-2 border-t pt-3"
				>
					<p class="break-words text-p-sm font-medium text-ink-gray-8">
						{{ question }}
					</p>
					<ul class="list-disc space-y-1 pl-5 text-p-sm text-ink-gray-7">
						<li v-for="point in points" :key="point">{{ point }}</li>
					</ul>
					<label
						class="jv-macro-handover-keep flex cursor-pointer items-start gap-2 text-p-sm text-ink-gray-8"
					>
						<input
							v-model="keep"
							type="checkbox"
							class="mt-[3px] h-3.5 w-3.5 shrink-0 rounded-sm border-outline-gray-4 bg-surface-white text-ink-gray-9 transition hover:border-outline-gray-5 focus:ring-0 focus:ring-offset-0 focus-visible:ring-2 focus-visible:ring-outline-gray-3"
							:disabled="saving"
						/>
						<span
							>Keep it with {{ ownerName }} too
							<span class="block text-ink-gray-6"
								>Gives everyone a copy instead of moving it.</span
							></span
						>
					</label>
				</div>
				<ErrorMessage v-if="error" :message="error" />
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-macro-handover-confirm"
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
// (the server refuses a macro that changed hands since). The first person picked is
// handed the macro and each of the others gets a copy; with "Keep" the macro stays
// and everyone picked gets a copy. The pane re-reads its list when told (`handed`,
// and `failed`: a hand-over can stop at a run that would not stop and leave the
// macro on hold, which the list should show; the pane then passes the owner it
// re-read, so a retry is not refused as "changed hands" again, or closes this with
// the message when the macro is no longer listed). A refusal stays in the dialog,
// next to the choice it is about. While the hand-over is in flight the dialog stays
// open: its answer belongs here.
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, toast } from "frappe-ui";
import { adminHandover, adminHandoverTargets } from "@/api/macrosAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

const SEARCH_WAIT_MS = 300;
// How many people one hand-over may name; the server holds the same limit.
const MAX_PEOPLE = 10;

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
// The people picked, in the order picked, as whole rows: a pick stays when a new
// search no longer lists it, so several can be gathered from several searches.
const picked = ref([]);
const keep = ref(false);
const saving = ref(false);
const error = ref("");
const bodyEl = ref(null);

const macroLabel = computed(() => props.macroName || props.name);
const title = computed(() => `Hand over “${macroLabel.value}”`);
const ownerName = computed(() => props.ownerLabel || "its owner");
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
		? `Give ${to} a copy of “${macroLabel.value}” from ${ownerName.value}?`
		: `Hand “${macroLabel.value}” from ${ownerName.value} to ${to}?`;
});
const points = computed(() => {
	if (keep.value)
		return [
			`${
				many.value ? "Each of them gets" : "They get"
			} their own copy; the original stays as it is.`,
			"A copy arrives switched off, unscheduled and not armed.",
			"A copy has no summary, no skills on its steps, no shares and no assignments.",
			"Everyone is told.",
		];
	if (many.value)
		return [
			"The first person picked gets it; each of the others gets their own copy.",
			"It and the copies arrive switched off, unscheduled and not armed.",
			"Its summary, the skills on its steps, its shares and its assignments are cleared.",
			"A run that is going now is stopped.",
			"Everyone is told.",
			"Its past runs stay with the current owner.",
		];
	return [
		"It arrives switched off, unscheduled and not armed.",
		"Its summary, the skills on its steps, its shares and its assignments are cleared.",
		"A run that is going now is stopped.",
		"Both of them are told.",
		"Its past runs stay with the current owner.",
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
	if (max.value && Number(u.macros) >= max.value) return `has ${u.macros} macros, the most`;
	return "";
}
const isPicked = (u) => picked.value.some((p) => p.user === u.user);
function unpick(u) {
	picked.value = picked.value.filter((p) => p.user !== u.user);
}
// Unique per dialog, for the reasons' and the field's ids.
const idBase = `jv-macro-handover-${Math.random().toString(36).slice(2, 10)}`;
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
	if (event.target.checked && !isPicked(u)) picked.value = [...picked.value, u];
	else if (!event.target.checked) unpick(u);
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
		// A pick the new results show is refreshed from them (its macro count may
		// have moved); one they do not show is kept as it was.
		picked.value = picked.value.map((p) => users.value.find((u) => u.user === p.user) || p);
	} catch (e) {
		if (mine !== latest) return;
		users.value = [];
		more.value = false;
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
		picked.value = [];
		keep.value = false;
		error.value = "";
		loaded.value = false;
		load();
		// The dialog puts focus on its close button: typing a name would press it.
		nextTick(() =>
			requestAnimationFrame(() => {
				const field = bodyEl.value && bodyEl.value.querySelector("input[type=text]");
				if (field) field.focus();
			})
		);
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	clearTimeout(searchTimer);
	latest++;
});

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
	try {
		const res =
			(await adminHandover(
				props.name,
				to.length === 1 ? to[0].user : to.map((u) => u.user),
				props.owner,
				keeping
			)) || {};
		const stopped = Number(res.stopped_runs) || 0;
		const copies = Array.isArray(res.copies) ? res.copies : [];
		const failed = Array.isArray(res.copy_failed) ? res.copy_failed : [];
		// A toast renders HTML: every name goes in escaped.
		const label = escapeHtml(macroLabel.value);
		const parts = [];
		if (res.handed_over !== false && !keeping)
			parts.push(
				`Handed “${label}” to ${escapeHtml(byUser(res.new_owner || to[0].user))}` +
					(stopped ? `; ${stopped} run${stopped === 1 ? "" : "s"} stopped` : "")
			);
		if (copies.length)
			parts.push(
				`${parts.length ? "copies went to" : `Gave a copy of “${label}” to`} ` +
					escapeHtml(listed(copies.map((c) => byUser(c.user))))
			);
		if (parts.length) toast.success(parts.join("; "));
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
			copies,
		});
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
