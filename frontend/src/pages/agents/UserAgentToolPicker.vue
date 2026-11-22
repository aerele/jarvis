<template>
	<div class="flex flex-col gap-1">
		<span class="block text-xs text-ink-gray-5">Tools</span>
		<!-- Search box and an inline list of toggles, grouped by app: the roles picker's
		     shape (SkillRolesField). Not the frappe-ui Autocomplete, whose search popover
		     is portaled outside a Dialog and cannot be clicked there
		     (PromotionRequestDialog). -->
		<FormControl
			type="text"
			placeholder="Search tools"
			:modelValue="query"
			:disabled="disabled"
			@update:modelValue="(v) => (query = v)"
		/>
		<div class="max-h-56 overflow-y-auto rounded border" role="group" aria-label="Tools">
			<template v-for="g in visibleGroups" :key="g.app">
				<div class="bg-surface-gray-1 px-3 py-1 text-xs font-medium text-ink-gray-6">
					{{ g.label }}
				</div>
				<button
					v-for="t in g.tools"
					:key="t.id"
					type="button"
					role="checkbox"
					:aria-checked="isPicked(t.id)"
					:disabled="disabled || (!isPicked(t.id) && atMax)"
					class="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-surface-gray-2 disabled:opacity-50"
					:class="
						isPicked(t.id)
							? 'bg-surface-gray-3 font-medium text-ink-gray-9'
							: 'text-ink-gray-7'
					"
					@click="toggle(t.id)"
				>
					<span class="flex min-w-0 items-center gap-2">
						<span class="truncate">{{ t.label }}</span>
						<Badge
							v-if="t.kind === 'write'"
							variant="subtle"
							theme="gray"
							label="Writes"
						/>
					</span>
					<FeatherIcon
						v-if="isPicked(t.id)"
						name="check"
						class="h-4 w-4 shrink-0 text-ink-gray-7"
					/>
				</button>
			</template>
			<p v-if="loading" class="px-3 py-2 text-p-sm text-ink-gray-5">Loading tools...</p>
			<p v-else-if="!visibleGroups.length" class="px-3 py-2 text-p-sm text-ink-gray-5">
				No tools match your search.
			</p>
		</div>
		<p class="text-p-sm text-ink-gray-5">
			{{ modelValue.length }} of {{ maxTools }} tools picked. The agent can only use tools
			you pick, and only with your permissions.
		</p>
	</div>
</template>

<script setup>
// The tools picker of the My agents dialog. `groups` is list_pickable_tools().groups
// ([{app, label, tools: [{id, label, kind}]}]); the model is the list of picked ids.
import { computed, ref } from "vue";
import { Badge, FeatherIcon, FormControl } from "frappe-ui";

const props = defineProps({
	modelValue: { type: Array, default: () => [] },
	groups: { type: Array, default: () => [] },
	maxTools: { type: Number, default: 64 },
	disabled: { type: Boolean, default: false },
	loading: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);

const query = ref("");
const isPicked = (id) => props.modelValue.includes(id);
const atMax = computed(() => props.modelValue.length >= props.maxTools);
const visibleGroups = computed(() => {
	const q = query.value.trim().toLowerCase();
	if (!q) return props.groups.filter((g) => g.tools.length);
	return props.groups
		.map((g) => ({
			...g,
			tools: g.tools.filter(
				(t) => t.label.toLowerCase().includes(q) || t.id.toLowerCase().includes(q)
			),
		}))
		.filter((g) => g.tools.length);
});

function toggle(id) {
	if (props.disabled) return;
	if (isPicked(id)) {
		emit(
			"update:modelValue",
			props.modelValue.filter((x) => x !== id)
		);
	} else if (!atMax.value) {
		emit("update:modelValue", [...props.modelValue, id]);
	}
}
</script>
