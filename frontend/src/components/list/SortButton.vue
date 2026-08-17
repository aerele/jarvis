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
					<!-- A portal-free radio menu, NOT a frappe-ui Select: that renders its
					     options through a reka SelectPortal, whose click counts as OUTSIDE
					     this Popover and dismisses it before a field can be picked - so only
					     the default field was ever reachable. In-popover buttons stay in the
					     Popover's own DOM, so it stays open until pickField() closes it. -->
					<div
						id="sort-by-heading"
						class="px-2 pb-1 text-xs font-medium text-ink-gray-5"
					>
						Sort by
					</div>
					<div role="menu" aria-labelledby="sort-by-heading" class="flex flex-col">
						<button
							v-for="opt in sortOptions"
							:key="opt.value"
							type="button"
							role="menuitemradio"
							:aria-checked="opt.value === currentField"
							class="flex items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-base text-ink-gray-8 hover:bg-surface-gray-2 focus:outline-none focus-visible:bg-surface-gray-2"
							@click="pickField(opt.value, close)"
						>
							<span class="truncate">{{ opt.label }}</span>
							<FeatherIcon
								v-if="opt.value === currentField"
								name="check"
								class="size-4 text-ink-gray-7"
							/>
						</button>
					</div>
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
import { Popover, Button, FeatherIcon } from "frappe-ui";

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

// The field the menu marks as selected: the active sort, else the page default.
const currentField = computed(() => props.sort.field || props.defaultSort.field || "");

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
