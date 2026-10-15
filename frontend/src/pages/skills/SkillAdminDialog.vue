<template>
	<!-- One skill of another user, to look at. Nothing here edits or saves it: an admin
	     does not act as the owner. A dialog over the Skills page's Admin tab: Escape
	     closes it and focus returns to the row that opened it. -->
	<Dialog v-model="show" :options="{ title, size: '3xl' }">
		<template #body-content>
			<!-- Deleted since the list was loaded: nothing to retry, only to close. The
			     tab is told (`gone`) and re-reads its list. -->
			<div
				v-if="gone"
				class="jv-skill-admin-gone flex flex-col items-center gap-3 py-8 text-center"
				role="alert"
			>
				<FeatherIcon name="trash-2" class="size-8 text-ink-gray-4" />
				<span class="text-base text-ink-gray-6">This skill was deleted.</span>
			</div>

			<div
				v-else-if="error"
				class="flex flex-col items-center gap-3 py-8 text-center"
				role="alert"
			>
				<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
				<span class="text-base text-ink-gray-6">{{ error }}</span>
				<Button
					variant="subtle"
					label="Try again"
					iconLeft="refresh-cw"
					:loading="loading"
					@click="load"
				/>
			</div>

			<p v-else-if="!skill" class="text-p-base text-ink-gray-6" role="status">Loading…</p>

			<!-- The one scroller of the dialog (up and down only). Focusable, so the
			     keyboard can scroll a long skill. -->
			<div
				v-else
				class="jv-skill-admin-body flex max-h-[65vh] flex-col gap-5 overflow-y-auto overflow-x-hidden pr-1"
				tabindex="0"
				role="region"
				aria-label="Skill details"
			>
				<p class="jv-skill-admin-note text-p-sm text-ink-gray-6">
					You are reading this as an administrator. These are {{ ownerText }}'s own
					instructions, shown read-only as plain text.
				</p>

				<dl class="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
					<dt class="text-ink-gray-5">Owner</dt>
					<dd class="break-words text-ink-gray-8">{{ ownerText }}</dd>
					<dt class="text-ink-gray-5">State</dt>
					<dd class="flex flex-wrap items-center gap-1">
						<Badge
							variant="subtle"
							:theme="skill.enabled ? 'green' : 'gray'"
							:label="enabledLabel(skill.enabled)"
						/>
						<Badge variant="subtle" theme="gray" :label="audienceLabel(skill)" />
						<Badge
							v-if="skill.allow_approve_run"
							variant="subtle"
							theme="orange"
							label="Writes without asking"
						/>
						<span v-if="skill.allow_approve_run" class="text-xs text-ink-gray-5">
							{{ WRITES_HELP }}
						</span>
						<Badge
							v-if="skill.admin_hold"
							class="jv-skill-admin-dialog-held"
							variant="subtle"
							theme="red"
							label="On hold"
						/>
					</dd>
					<template v-if="skill.admin_hold">
						<dt class="text-ink-gray-5">Hold reason</dt>
						<dd
							class="jv-skill-admin-dialog-reason whitespace-pre-wrap break-words text-ink-gray-8"
						>
							{{ skill.admin_hold_reason || "None given" }}
						</dd>
					</template>
					<dt class="text-ink-gray-5">Audience</dt>
					<dd class="break-words text-ink-gray-8">{{ audienceDetail(skill) }}</dd>
					<template v-if="skill.description">
						<dt class="text-ink-gray-5">Description</dt>
						<dd class="whitespace-pre-wrap break-words text-ink-gray-8">
							{{ skill.description }}
						</dd>
					</template>
					<template v-if="skill.lineage">
						<dt class="text-ink-gray-5">Copied from</dt>
						<dd class="jv-skill-admin-lineage break-words text-ink-gray-8">
							{{ lineageText }}
						</dd>
					</template>
					<dt class="text-ink-gray-5">Macros</dt>
					<dd class="jv-skill-admin-macros text-ink-gray-8">
						{{ macroUse(skill.used_by_macros) }}
					</dd>
				</dl>

				<section aria-labelledby="jv-skill-admin-instructions">
					<h3
						id="jv-skill-admin-instructions"
						class="mb-1 text-sm font-medium text-ink-gray-7"
					>
						Instructions
					</h3>
					<!-- Another user's free text: printed as it is, never through the
					     Markdown renderer. -->
					<pre
						v-if="skill.instructions"
						class="jv-skill-admin-text"
						:class="PRE"
						tabindex="0"
						aria-label="Instructions"
						>{{ skill.instructions }}</pre
					>
					<p v-else class="text-sm text-ink-gray-5">No instructions.</p>
				</section>

				<section aria-labelledby="jv-skill-admin-shares">
					<h3
						id="jv-skill-admin-shares"
						class="mb-1 text-sm font-medium text-ink-gray-7"
					>
						Shared with
					</h3>
					<ul v-if="skill.shared_with.length" class="jv-skill-admin-shares text-sm">
						<li v-for="u in skill.shared_with" :key="u.user" class="break-words">
							{{
								u.full_name && u.full_name !== u.user
									? `${u.full_name} (${u.user})`
									: u.user
							}}
						</li>
					</ul>
					<p v-else class="text-sm text-ink-gray-5">Nobody.</p>
				</section>

				<section v-if="skill.allowed_roles.length" aria-labelledby="jv-skill-admin-roles">
					<h3 id="jv-skill-admin-roles" class="mb-1 text-sm font-medium text-ink-gray-7">
						Roles that can use it
					</h3>
					<p class="jv-skill-admin-roles break-words text-sm text-ink-gray-8">
						{{ skill.allowed_roles.join(", ") }}
					</p>
				</section>

				<section aria-labelledby="jv-skill-admin-history">
					<h3
						id="jv-skill-admin-history"
						class="mb-1 text-sm font-medium text-ink-gray-7"
					>
						History
					</h3>
					<p v-if="!skill.history.length" class="text-sm text-ink-gray-5">
						No administrator has acted on this skill.
					</p>
					<!-- A long reason makes the table wider than the dialog: it scrolls
					     sideways here, in its own box. -->
					<div
						v-else
						class="jv-skill-admin-history overflow-x-auto"
						tabindex="0"
						role="region"
						aria-label="History"
					>
						<table class="w-full text-left text-sm">
							<thead class="text-xs text-ink-gray-5">
								<tr>
									<th scope="col" class="py-1 pr-3 font-medium">When</th>
									<th scope="col" class="py-1 pr-3 font-medium">Who</th>
									<th scope="col" class="py-1 pr-3 font-medium">What</th>
									<th scope="col" class="py-1 font-medium">Reason</th>
								</tr>
							</thead>
							<tbody>
								<tr
									v-for="(h, i) in skill.history"
									:key="i"
									class="border-t align-top"
								>
									<td
										class="whitespace-nowrap py-1.5 pr-3 text-ink-gray-8"
										:title="exactDate(h.at)"
									>
										{{ timeAgo(h.at) }}
									</td>
									<td class="py-1.5 pr-3 text-ink-gray-8">
										{{ h.admin_name || h.admin }}
									</td>
									<td class="whitespace-nowrap py-1.5 pr-3 text-ink-gray-8">
										{{ verbText(h.verb, h.new_owner_name || h.new_owner) }}
									</td>
									<td
										class="min-w-40 whitespace-pre-wrap py-1.5 text-ink-gray-7 [overflow-wrap:anywhere]"
									>
										{{ h.reason || "-" }}
									</td>
								</tr>
							</tbody>
						</table>
					</div>
				</section>
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end">
				<Button label="Close" @click="show = false" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// The read-only view of one skill for a Jarvis Admin: its settings, its instructions
// as plain text, who it is shared with, where it came from, the admin history and
// how many macros use it. Every value is rendered as text ({{ }}), never as HTML or
// Markdown: the instructions are another user's free text.
import { computed, ref, watch } from "vue";
import { Badge, Button, Dialog, FeatherIcon } from "frappe-ui";
import { adminGetSkill } from "@/api/skillsAdmin";
import { errMessage } from "@/lib/errors";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	WRITES_HELP,
	audienceDetail,
	audienceLabel,
	enabledLabel,
	macroUse,
	ownerText as ownerOf,
	skillLabel,
	verbText,
} from "./skillAdminDisplay";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	name: { type: String, required: true },
});
// `gone`: the skill no longer exists (deleted since the list was loaded).
const emit = defineEmits(["update:modelValue", "gone"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const PRE =
	"max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-gray-2 p-3 text-xs text-ink-gray-8";

const skill = ref(null);
const loading = ref(false);
const error = ref("");
const gone = ref(false);

// Only the newest request may fill the dialog: it is re-pointed at another row
// without being unmounted.
let latest = 0;
async function load() {
	const mine = ++latest;
	loading.value = true;
	error.value = "";
	gone.value = false;
	try {
		const res = (await adminGetSkill(props.name)) || {};
		if (mine !== latest) return;
		skill.value = {
			...res,
			shared_with: res.shared_with || [],
			allowed_roles: res.allowed_roles || [],
			history: res.history || [],
		};
	} catch (e) {
		if (mine !== latest) return;
		skill.value = null;
		// Not an error to try again: its owner deleted it after the list was loaded.
		if (e && (e.status === 404 || e.exc_type === "DoesNotExistError")) {
			gone.value = true;
			emit("gone", props.name);
			return;
		}
		error.value = errMessage(e, "Could not load this skill.");
	} finally {
		if (mine === latest) loading.value = false;
	}
}

watch(
	() => [props.modelValue, props.name],
	([open]) => {
		if (!open) return;
		skill.value = null;
		gone.value = false;
		load();
	},
	{ immediate: true }
);

const title = computed(() => (skill.value ? skillLabel(skill.value) : "Skill"));
const ownerText = computed(() => (skill.value ? ownerOf(skill.value) : "its owner"));
const lineageText = computed(() => {
	const l = skill.value && skill.value.lineage;
	if (!l) return "";
	const by = l.author_name || l.author;
	return by ? `${l.source_skill}, written by ${by}` : l.source_skill;
});
</script>
