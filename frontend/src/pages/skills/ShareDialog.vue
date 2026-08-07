<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: `Share “${skill.skill_name || skill.name}”`, size: 'md' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<div class="text-p-sm text-ink-gray-6">
				They can use this skill in chat, but can’t edit or re-share it.
			</div>

			<div v-if="loading" class="py-8 text-center text-sm text-ink-gray-5">
				Loading people…
			</div>
			<template v-else>
				<!-- selected chips -->
				<div v-if="selected.length" class="mt-3 flex flex-wrap gap-1.5">
					<div
						v-for="id in selected"
						:key="id"
						class="flex h-6 items-center gap-1.5 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
					>
						<span class="max-w-[160px] truncate">{{ userLabel(id) }}</span>
						<Button variant="ghost" icon="x" class="!h-4 !w-4" @click="toggle(id)" />
					</div>
				</div>

				<FormControl
					type="text"
					class="mt-3"
					placeholder="Search people…"
					:modelValue="search"
					@update:modelValue="(v) => (search = v)"
				>
					<template #prefix>
						<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
					</template>
				</FormControl>

				<div v-if="!candidates.length" class="py-6 text-center text-sm text-ink-gray-5">
					No other users to share with yet.
				</div>
				<div v-else-if="!matches.length" class="py-6 text-center text-sm text-ink-gray-5">
					No people match “{{ search }}”.
				</div>
				<div v-else class="mt-2 max-h-64 overflow-y-auto">
					<button
						v-for="u in matches"
						:key="u.name"
						class="flex w-full items-center gap-3 rounded px-2 py-1.5 text-left hover:bg-surface-gray-2"
						@click="toggle(u.name)"
					>
						<Avatar size="md" :label="u.full_name || u.name" />
						<span class="flex min-w-0 flex-1 flex-col">
							<span class="truncate text-base text-ink-gray-8">{{
								u.full_name || u.name
							}}</span>
							<span class="truncate text-sm text-ink-gray-5">{{ u.name }}</span>
						</span>
						<FeatherIcon
							v-if="isSelected(u.name)"
							name="check"
							class="size-4 shrink-0 text-ink-gray-7"
						/>
					</button>
				</div>
			</template>
		</template>

		<template #actions>
			<div class="flex items-center gap-2">
				<Button
					variant="solid"
					label="Save"
					:loading="saving"
					:disabled="loading"
					@click="save"
				/>
				<Button
					label="Cancel"
					:disabled="saving"
					@click="emit('update:modelValue', false)"
				/>
				<span class="ml-auto text-sm text-ink-gray-5">
					{{ selected.length }}
					{{ selected.length === 1 ? "person" : "people" }} selected
				</span>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// ShareDialog - skill sharing manager (DESIGN-V3 §6.2; round-2 semantics
// ported): search over listShareableUsers, checked rows + selected chips,
// Save replaces the whole share list via shareCustomSkill (replace semantics).
import { ref, computed, watch } from "vue";
import { Dialog, Button, FormControl, Avatar, FeatherIcon, toast } from "frappe-ui";
import * as api from "@/api";
import { errHtml } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	skill: { type: Object, default: () => ({ name: "", skill_name: "" }) }, // {name, skill_name}
});

const emit = defineEmits(["update:modelValue", "saved"]);

const loading = ref(false);
const saving = ref(false);
const search = ref("");
const candidates = ref([]); // [{name, full_name}]
const selected = ref([]); // user ids

watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
	}
);

async function load() {
	search.value = "";
	selected.value = [];
	candidates.value = [];
	saving.value = false;
	loading.value = true;
	try {
		const [cand, sharesRes] = await Promise.all([
			api.listShareableUsers(),
			api.getSkillShares(props.skill.name),
		]);
		candidates.value = cand || [];
		const current = (sharesRes && sharesRes.users) || [];
		selected.value = current.map((u) => u.name);
		// keep already-shared users visible even if they fall outside the
		// shareable-candidates list (e.g. since-disabled accounts)
		const known = new Set(candidates.value.map((u) => u.name));
		for (const u of current) {
			if (!known.has(u.name)) {
				candidates.value.push({ name: u.name, full_name: u.full_name || u.name });
				known.add(u.name);
			}
		}
	} catch (e) {
		toast.error(errHtml(e));
		emit("update:modelValue", false);
	} finally {
		loading.value = false;
	}
}

const matches = computed(() => {
	const q = search.value.trim().toLowerCase();
	if (!q) return candidates.value;
	return candidates.value.filter(
		(u) =>
			(u.full_name || "").toLowerCase().includes(q) ||
			(u.name || "").toLowerCase().includes(q)
	);
});

function isSelected(id) {
	return selected.value.includes(id);
}
function toggle(id) {
	if (isSelected(id)) selected.value = selected.value.filter((x) => x !== id);
	else selected.value = [...selected.value, id];
}
function userLabel(id) {
	const u = candidates.value.find((x) => x.name === id);
	return (u && u.full_name) || id;
}

async function save() {
	if (saving.value) return;
	saving.value = true;
	try {
		await api.shareCustomSkill(props.skill.name, [...selected.value]); // replace semantics
		toast.success("Sharing updated");
		emit("saved");
		emit("update:modelValue", false);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		saving.value = false;
	}
}
</script>
