<template>
	<div class="flex items-center">
		<!-- Direction toggle is ALWAYS visible now, so a list can be flipped
		     ascending/descending even at its page-default sort. Previously it only
		     appeared once a non-default sort was active, which left no way to
		     reverse the default column (e.g. oldest-first on a list that defaults
		     to newest-first) without first switching to another field. Keeping it
		     out of the popover also dodges the reka-Select-in-Popover dismissal
		     (a select's portal counts as an outside click). -->
		<Button
			:icon="dir === 'asc' ? 'arrow-up' : 'arrow-down'"
			class="rounded-r-none border-r"
			:tooltip="
				dir === 'asc'
					? 'Sorted ascending - click for descending'
					: 'Sorted descending - click for ascending'
			"
			@click="toggleDir"
		/>
		<Popover placement="bottom-end">
			<template #target="{ togglePopover }">
				<Button
					:label="isDefault ? 'Sort' : fieldLabel"
					class="rounded-l-none"
					@click="togglePopover()"
				/>
			</template>
			<template #body="{ close }">
				<div
					class="my-2 min-w-60 rounded-lg bg-surface-modal p-2 shadow-2xl ring-1 ring-black ring-opacity-5"
				>
					<FormControl
						type="select"
						label="Sort by"
						:options="sortOptions"
						:modelValue="sort.field || defaultSort.field || ''"
						@update:modelValue="(v) => pickField(v, close)"
					/>
					<div class="mt-2 flex justify-end border-t pt-2">
						<Button
							variant="ghost"
							label="Clear Sort"
							class="!text-ink-gray-5"
							@click="reset(close)"
						/>
					</div>
				</div>
			</template>
		</Popover>
		<Button
			v-if="!isDefault"
			variant="ghost"
			icon="x"
			:tooltip="'Reset sort'"
			@click="reset()"
		/>
	</div>
</template>

<script setup>
// SortButton - single field + direction (DESIGN-V3 §5.4, D15): an always-visible
// asc/desc toggle joined to the field button ("Sort" at the page default, the
// field label once chosen); a ghost x reset appears once a non-default sort is
// active. Emits update:sort {field, dir}.
import { computed } from "vue";
import { Popover, Button, FormControl } from "frappe-ui";

const props = defineProps({
	sortOptions: { type: Array, default: () => [] }, // [{label, value}]
	sort: { type: Object, default: () => ({ field: "", dir: "" }) },
	defaultSort: { type: Object, default: () => ({ field: "", dir: "" }) },
});

const emit = defineEmits(["update:sort"]);

const isDefault = computed(
	() =>
		(props.sort.field || "") === (props.defaultSort.field || "") &&
		(props.sort.dir || "") === (props.defaultSort.dir || "")
);

// Effective direction: the active sort's dir, else the page default, else desc.
// The always-visible toggle needs a value even before the user has changed sort.
const dir = computed(() => props.sort.dir || props.defaultSort.dir || "desc");

const fieldLabel = computed(() => {
	const opt = (props.sortOptions || []).find((o) => o.value === props.sort.field);
	return (opt && opt.label) || props.sort.field || "Sort";
});

function toggleDir() {
	// Fall back to the default field so toggling from the untouched default state
	// (where sort.field may not yet be set by the page) still emits a real field.
	emit("update:sort", {
		field: props.sort.field || props.defaultSort.field || "",
		dir: dir.value === "asc" ? "desc" : "asc",
	});
}

function pickField(field, close) {
	if (!field) return;
	emit("update:sort", { field, dir: props.sort.dir || props.defaultSort.dir || "asc" });
	if (close) close();
}

function reset(close) {
	emit("update:sort", {
		field: props.defaultSort.field || "",
		dir: props.defaultSort.dir || "",
	});
	if (close) close();
}
</script>
