<template>
	<Dialog v-model="show" :options="{ title: dialogTitle, size: 'md' }">
		<template #body-content>
			<div class="flex flex-col gap-3">
				<p class="text-sm text-ink-gray-6">
					Promotion widens who can {{ verb }} this {{ noun }}. It stays private to you
					until a reviewer approves the request.
				</p>
				<FormControl
					type="select"
					label="Promote to"
					:options="TO_SCOPE_OPTIONS"
					:modelValue="toScope"
					@update:modelValue="(v) => (toScope = v)"
				/>
				<p class="text-p-sm text-ink-gray-5">{{ scopeHelp[toScope] }}</p>

				<div v-if="toScope === 'Role'" class="flex flex-col gap-1">
					<span class="block text-xs text-ink-gray-5">{{
						multiple ? "Roles" : "Role"
					}}</span>
					<template v-if="roleOptions.length">
						<!-- Inline search + list, NOT the frappe-ui Autocomplete: its search
						     box renders in a popover portaled OUTSIDE this reka-ui Dialog, where
						     the Dialog's focus scope left it unclickable. A plain input and an
						     inline list both sit inside the Dialog, so search AND selection work. -->
						<FormControl
							type="text"
							placeholder="Search your roles"
							:modelValue="roleQuery"
							@update:modelValue="(v) => (roleQuery = v)"
						/>
						<div
							class="max-h-44 overflow-y-auto rounded border"
							:role="multiple ? 'group' : 'listbox'"
						>
							<button
								v-for="r in filteredRoles"
								:key="r"
								type="button"
								:role="multiple ? 'checkbox' : 'option'"
								:aria-checked="multiple ? isSelected(r) : undefined"
								:aria-selected="multiple ? undefined : isSelected(r)"
								class="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-surface-gray-2"
								:class="
									isSelected(r)
										? 'bg-surface-gray-3 font-medium text-ink-gray-9'
										: 'text-ink-gray-7'
								"
								@click="pick(r)"
							>
								<span>{{ r }}</span>
								<FeatherIcon
									v-if="isSelected(r)"
									name="check"
									class="h-4 w-4 text-ink-gray-7"
								/>
							</button>
							<p
								v-if="!filteredRoles.length"
								class="px-3 py-2 text-p-sm text-ink-gray-5"
							>
								No roles match your search.
							</p>
						</div>
						<p v-if="multiple && selected.length" class="text-p-sm text-ink-gray-5">
							{{ selected.length }} role{{ selected.length === 1 ? "" : "s" }}
							selected
						</p>
					</template>
					<p v-else-if="!rolesLoading" class="text-p-sm text-ink-gray-5">
						You hold no roles that can be targeted - promote to the whole org instead,
						or ask an admin to add you to the role first.
					</p>
				</div>

				<FormControl
					type="textarea"
					label="Why? (optional)"
					:rows="2"
					placeholder="A short note so the reviewer knows why this should be shared."
					:modelValue="note"
					@update:modelValue="(v) => (note = v)"
				/>
			</div>
		</template>
		<template #actions>
			<div class="flex items-center gap-2">
				<Button
					variant="solid"
					label="Send request"
					:loading="busy"
					:disabled="!canSubmit || busy"
					@click="submit"
				/>
				<Button label="Cancel" @click="show = false" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// Shared "Request promotion…" dialog (Skills-area promotion surfacing) — used by
// SkillDetail and WikiPageDialog so the requester flow reads identically for a
// skill and a wiki page. Emits `submit({to_scope, target_role, note})`; the host
// owns the API call (request_skill_promotion / request_wiki_promotion) and the
// busy flag. Role options are the requester's OWN targetable roles
// (promotable_target_roles) — a requester widens to a team they belong to.
import { ref, computed, watch } from "vue";
import { Button, Dialog, FeatherIcon, FormControl, toast } from "frappe-ui";
import { promotableTargetRoles } from "@/api/skills";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	noun: { type: String, default: "skill" }, // "skill" | "page"
	busy: { type: Boolean, default: false },
	// Multiselect role targeting (skills only). One request names several roles and
	// the whole set becomes the skill's audience on approval. Wiki pages stay
	// one-role-per-page, so WikiPageDialog leaves this false (single-select).
	multiple: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "submit"]);

const TO_SCOPE_OPTIONS = [
	{ label: "A role (a team)", value: "Role" },
	{ label: "The whole organisation", value: "Org" },
];
// noun-keyed verb so the shared dialog reads naturally for a skill ("use") and
// for a wiki page ("view") — SPX-10.
const VERB = { skill: "use", page: "view" };

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const toScope = ref("Org");
// The chosen role(s). Always an array so the single- and multi-select paths share
// one model; single-select just holds at most one. submit() derives target_role
// (the primary, for backward compatibility + wiki) from selected[0].
const selected = ref([]);
const note = ref("");
const roles = ref([]);
const rolesLoading = ref(false);

function isSelected(r) {
	return selected.value.includes(r);
}
// Single-select replaces the choice; multiselect toggles the role in/out.
function pick(r) {
	if (props.multiple) {
		selected.value = isSelected(r)
			? selected.value.filter((x) => x !== r)
			: [...selected.value, r];
	} else {
		selected.value = [r];
	}
}

const dialogTitle = computed(() => `Request promotion — ${props.noun}`);
const roleOptions = computed(() => roles.value.map((r) => ({ label: r, value: r })));
const roleQuery = ref("");
// Client-side filter for the inline role list (see the template note on why this
// is a plain input + list rather than the portaled Autocomplete search).
const filteredRoles = computed(() => {
	const q = roleQuery.value.trim().toLowerCase();
	return q ? roles.value.filter((r) => r.toLowerCase().includes(q)) : roles.value;
});
const canSubmit = computed(
	() => !!toScope.value && (toScope.value !== "Role" || selected.value.length > 0)
);
const verb = computed(() => VERB[props.noun] || "use");
const scopeHelp = computed(() => ({
	Role: props.multiple
		? `Everyone who holds ANY of the chosen roles will be able to ${verb.value} it.`
		: `Everyone who holds the chosen role will be able to ${verb.value} it.`,
	Org: `Everyone in your organisation will be able to ${verb.value} it.`,
}));

async function loadRoles() {
	if (roles.value.length || rolesLoading.value) return;
	rolesLoading.value = true;
	try {
		const res = await promotableTargetRoles();
		roles.value = (res && res.roles) || [];
	} catch {
		roles.value = [];
	} finally {
		rolesLoading.value = false;
	}
}

// Reset the form each time the dialog opens (and lazily fetch the role list).
// Default to Org (SPX-3): it's always submittable. Role pre-select was a dead
// end for a plain user — promotable_target_roles excludes the baseline
// Jarvis User role, so a Role default opened to an empty picker + disabled Send.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		toScope.value = "Org";
		selected.value = [];
		roleQuery.value = "";
		note.value = "";
		loadRoles();
	}
);

function submit() {
	if (!canSubmit.value) {
		toast.error(
			props.multiple
				? "Pick at least one role for role-scope promotion."
				: "Pick a role for role-scope promotion."
		);
		return;
	}
	const isRole = toScope.value === "Role";
	const payload = {
		to_scope: toScope.value,
		// The primary role — first of the selection — kept for backward
		// compatibility and the single-select (wiki) host.
		target_role: isRole ? selected.value[0] || "" : "",
		note: (note.value || "").trim(),
	};
	// Only the multiselect (skill) host sends the full set; the backend widens the
	// skill's audience to every listed role.
	if (props.multiple) payload.target_roles = isRole ? [...selected.value] : [];
	emit("submit", payload);
}
</script>
