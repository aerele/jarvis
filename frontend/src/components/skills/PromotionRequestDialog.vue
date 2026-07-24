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
					<span class="block text-xs text-ink-gray-5">Role</span>
					<!-- Autocomplete: same role-picker idiom the wiki create dialog
					     uses; options are the requester's OWN targetable roles. -->
					<Autocomplete
						placeholder="Search your roles"
						:options="roleOptions"
						:modelValue="targetRole"
						@update:modelValue="(v) => (targetRole = (v && v.value) || '')"
					/>
					<p
						v-if="!rolesLoading && !roleOptions.length"
						class="text-p-sm text-ink-gray-5"
					>
						You hold no roles that can be targeted — promote to the whole org instead,
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
import { Autocomplete, Button, Dialog, FormControl, toast } from "frappe-ui";
import { promotableTargetRoles } from "@/api/skills";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	noun: { type: String, default: "skill" }, // "skill" | "page"
	busy: { type: Boolean, default: false },
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
const targetRole = ref("");
const note = ref("");
const roles = ref([]);
const rolesLoading = ref(false);

const dialogTitle = computed(() => `Request promotion — ${props.noun}`);
const roleOptions = computed(() => roles.value.map((r) => ({ label: r, value: r })));
const canSubmit = computed(
	() => !!toScope.value && (toScope.value !== "Role" || !!targetRole.value)
);
const verb = computed(() => VERB[props.noun] || "use");
const scopeHelp = computed(() => ({
	Role: `Everyone who holds the chosen role will be able to ${verb.value} it.`,
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
		targetRole.value = "";
		note.value = "";
		loadRoles();
	}
);

function submit() {
	if (!canSubmit.value) {
		toast.error("Pick a role for role-scope promotion.");
		return;
	}
	emit("submit", {
		to_scope: toScope.value,
		target_role: toScope.value === "Role" ? targetRole.value : "",
		note: (note.value || "").trim(),
	});
}
</script>
