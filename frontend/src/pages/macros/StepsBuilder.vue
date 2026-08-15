<template>
	<div>
		<div class="flex flex-col gap-3">
			<template v-for="(st, si) in modelValue" :key="si">
				<div
					v-if="showIndicatorAbove(si)"
					class="h-0.5 rounded bg-[color:var(--ink-gray-9)]"
				/>
				<div
					class="space-y-2 rounded-md border p-3"
					:class="{ 'opacity-50': dragIdx === si }"
					@dragover.prevent="onDragOver(si)"
					@dragleave="onDragLeave(si)"
					@drop.prevent="onDrop(si)"
				>
					<div class="flex items-center gap-2">
						<span
							class="lucide-grip-vertical h-4 cursor-grab text-ink-gray-4"
							:draggable="!disabled"
							title="Drag to reorder"
							aria-hidden="true"
							@dragstart="onDragStart(si, $event)"
							@dragend="onDragEnd"
						/>
						<span class="text-sm font-medium text-ink-gray-8">Step {{ si + 1 }}</span>
						<div class="flex-1" />
						<Button
							variant="ghost"
							icon="arrow-up"
							:disabled="disabled || si === 0"
							:tooltip="'Move up'"
							@click="move(si, -1)"
						/>
						<Button
							variant="ghost"
							icon="arrow-down"
							:disabled="disabled || si === modelValue.length - 1"
							:tooltip="'Move down'"
							@click="move(si, 1)"
						/>
						<Button
							variant="ghost"
							icon="x"
							:disabled="disabled || modelValue.length <= 1"
							:tooltip="'Remove step'"
							@click="removeStep(si)"
						/>
					</div>
					<FormControl
						size="sm"
						type="text"
						placeholder="Optional label"
						:modelValue="st.label"
						:disabled="disabled"
						@update:modelValue="(v) => (st.label = v)"
					/>
					<FormControl
						type="textarea"
						:rows="3"
						placeholder="The prompt to send for this step…"
						:modelValue="st.prompt"
						:disabled="disabled"
						@update:modelValue="(v) => (st.prompt = v)"
					/>
					<div class="flex flex-wrap items-center gap-1.5">
						<span class="text-sm text-ink-gray-5">Skills</span>
						<div
							v-for="name in st.skills || []"
							:key="name"
							class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
						>
							<span class="truncate font-mono">/{{ skillLabel(name) }}</span>
							<Button
								variant="ghost"
								icon="x"
								class="!h-4 !w-4"
								:disabled="disabled"
								@click="removeSkill(st, name)"
							/>
						</div>
						<div class="min-w-[160px]">
							<Autocomplete
								:options="skillOptionsFor(st)"
								:modelValue="null"
								placeholder="Add skill…"
								@update:modelValue="(opt) => opt && addSkill(st, opt.value)"
							/>
						</div>
					</div>
				</div>
				<div
					v-if="showIndicatorBelow(si)"
					class="h-0.5 rounded bg-[color:var(--ink-gray-9)]"
				/>
			</template>
		</div>
		<button
			type="button"
			class="mt-3 h-9 w-full rounded-md border border-dashed border-outline-gray-3 text-base text-ink-gray-5 hover:bg-surface-gray-1"
			:disabled="disabled"
			@click="addStep"
		>
			＋ Add step
		</button>
	</div>
</template>

<script setup>
// StepsBuilder - the macro steps editor (DESIGN-V3 §6.3): step cards with a
// drag handle (HTML5 drag-reorder, ported from round-2 MacrosView), ↑/↓
// buttons as the accessible fallback, per-step skills chips + Autocomplete,
// dashed "Add step" footer. v-model on the steps array; field edits mutate
// the (parent-reactive) step objects in place, structural changes emit a new
// array.
import { ref, onMounted } from "vue";
import { Button, FormControl, Autocomplete, confirmDialog } from "frappe-ui";
import * as api from "@/api";

const props = defineProps({
	modelValue: { type: Array, default: () => [] }, // [{label, prompt, skills[]}]
	disabled: { type: Boolean, default: false },
});

const emit = defineEmits(["update:modelValue"]);

// ── per-step skills: own + shared-with-me, enabled only (a disabled skill
//    can't be invoked - round-2 parity) ────────────────────────────────────────
const skillRows = ref([]);
onMounted(async () => {
	try {
		skillRows.value = ((await api.listCustomSkills()) || []).filter((s) => s.enabled);
	} catch (e) {
		// picker stays empty; chips still render from the raw names
	}
});

function skillLabel(name) {
	const s = skillRows.value.find((r) => r.name === name);
	return (s && s.skill_name) || name;
}

function skillOptionsFor(st) {
	const taken = new Set(st.skills || []);
	return skillRows.value
		.filter((s) => !taken.has(s.name))
		.map((s) => ({ label: `/${s.skill_name}${s.mine ? "" : " · shared"}`, value: s.name }));
}

function addSkill(st, name) {
	if (props.disabled) return;
	if (!Array.isArray(st.skills)) st.skills = [];
	if (!st.skills.includes(name)) st.skills.push(name);
}

function removeSkill(st, name) {
	st.skills = (st.skills || []).filter((n) => n !== name);
}

// ── structural edits (emit a fresh array) ────────────────────────────────────
function addStep() {
	emit("update:modelValue", [...props.modelValue, { label: "", prompt: "", skills: [] }]);
}

function removeStep(si) {
	if (props.modelValue.length <= 1) return; // min 1 step
	const st = props.modelValue[si];
	const doRemove = () =>
		emit(
			"update:modelValue",
			props.modelValue.filter((_, i) => i !== si)
		);
	if ((st.prompt || "").trim()) {
		confirmDialog({
			title: "Remove step?",
			message: `Step ${si + 1} has a prompt - remove it anyway?`,
			onConfirm: ({ hideDialog }) => {
				doRemove();
				hideDialog();
			},
		});
	} else {
		doRemove();
	}
}

function move(si, delta) {
	const to = si + delta;
	if (to < 0 || to >= props.modelValue.length) return;
	const next = [...props.modelValue];
	const [it] = next.splice(si, 1);
	next.splice(to, 0, it);
	emit("update:modelValue", next);
}

// ── HTML5 drag-reorder (grip = drag source, whole card = drop target) ────────
const dragIdx = ref(null);
const dragOverIdx = ref(null);

function onDragStart(si, e) {
	if (props.disabled) return;
	dragIdx.value = si;
	if (e && e.dataTransfer) {
		e.dataTransfer.effectAllowed = "move";
		try {
			e.dataTransfer.setData("text/plain", String(si));
		} catch (err) {
			// some browsers throw on setData for non-standard types - ignore
		}
	}
}

function onDragOver(si) {
	if (dragIdx.value !== null) dragOverIdx.value = si;
}

function onDragLeave(si) {
	if (dragOverIdx.value === si) dragOverIdx.value = null;
}

function onDrop(si) {
	const from = dragIdx.value;
	dragIdx.value = null;
	dragOverIdx.value = null;
	if (from === null || from === si) return;
	const next = [...props.modelValue];
	const [it] = next.splice(from, 1);
	next.splice(si, 0, it);
	emit("update:modelValue", next);
}

function onDragEnd() {
	dragIdx.value = null;
	dragOverIdx.value = null;
}

// drop indicator sits where the dragged card will land (above the target when
// moving up, below it when moving down)
function showIndicatorAbove(si) {
	return dragIdx.value !== null && dragOverIdx.value === si && dragIdx.value > si;
}
function showIndicatorBelow(si) {
	return dragIdx.value !== null && dragOverIdx.value === si && dragIdx.value < si;
}
</script>
