<template>
	<div class="flex flex-col gap-1">
		<span class="block text-xs text-ink-gray-5">Roles that can use it</span>
		<!-- The promotion dialog's role picker: a plain search box and an inline list of
		     toggles (PromotionRequestDialog.vue). -->
		<FormControl
			type="text"
			placeholder="Search roles"
			:modelValue="query"
			:disabled="disabled"
			@update:modelValue="(v) => (query = v)"
		/>
		<div class="max-h-44 overflow-y-auto rounded border" role="group" aria-label="Roles">
			<button
				v-for="r in filtered"
				:key="r"
				type="button"
				role="checkbox"
				:aria-checked="isSelected(r)"
				:disabled="disabled"
				class="jv-skill-role flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-surface-gray-2"
				:class="
					isSelected(r)
						? 'bg-surface-gray-3 font-medium text-ink-gray-9'
						: 'text-ink-gray-7'
				"
				@click="toggle(r)"
			>
				<span>{{ r }}</span>
				<FeatherIcon v-if="isSelected(r)" name="check" class="h-4 w-4 text-ink-gray-7" />
			</button>
			<p v-if="!filtered.length" class="px-3 py-2 text-p-sm text-ink-gray-5">
				No roles match your search.
			</p>
		</div>
		<p v-if="!modelValue.length" class="text-p-sm text-ink-red-4" role="alert">
			Keep at least one role.
		</p>
		<p v-else class="text-p-sm text-ink-gray-5">
			Everyone who holds any of these roles can use this skill. You can add a role you hold
			yourself.
		</p>
	</div>
</template>

<script setup>
// A shared skill's roles, for a skill reviewer on its form (5c). The options are the
// roles the reviewer may add (promotable_target_roles) plus the ones it already has;
// the server checks the same rule.
import { computed, ref } from "vue";
import { FeatherIcon, FormControl } from "frappe-ui";

const props = defineProps({
	modelValue: { type: Array, default: () => [] },
	options: { type: Array, default: () => [] },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);

const query = ref("");
const isSelected = (r) => props.modelValue.includes(r);
const filtered = computed(() => {
	const q = query.value.trim().toLowerCase();
	return q ? props.options.filter((r) => r.toLowerCase().includes(q)) : props.options;
});

function toggle(r) {
	if (props.disabled) return;
	emit(
		"update:modelValue",
		isSelected(r) ? props.modelValue.filter((x) => x !== r) : [...props.modelValue, r]
	);
}
</script>
