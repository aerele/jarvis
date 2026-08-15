<template>
	<div class="flex items-center">
		<Popover placement="bottom-end">
			<template #target="{ togglePopover }">
				<Button
					label="Filter"
					iconLeft="filter"
					:class="activeCount ? 'rounded-r-none' : ''"
					@click="togglePopover()"
				>
					<template v-if="activeCount" #suffix>
						<div
							class="flex h-5 w-5 items-center justify-center rounded-[5px] bg-surface-white pt-px text-xs font-medium text-ink-gray-8 shadow-sm"
						>
							{{ activeCount }}
						</div>
					</template>
				</Button>
			</template>
			<template #body>
				<div
					class="my-2 min-w-72 rounded-lg bg-surface-modal p-2 shadow-2xl ring-1 ring-black ring-opacity-5"
				>
					<div
						v-if="!activeDefs.length"
						class="mb-3 flex h-7 items-center px-3 text-sm text-ink-gray-5"
					>
						Empty - Choose a field to filter by
					</div>
					<div v-else class="mb-3 flex flex-col gap-2">
						<div
							v-for="(def, i) in activeDefs"
							:key="def.key"
							class="flex items-center gap-2"
						>
							<div class="w-13 shrink-0 pl-2 text-end text-base text-ink-gray-5">
								{{ i === 0 ? "Where" : "And" }}
							</div>
							<div class="min-w-[110px] text-base text-ink-gray-8">
								{{ def.label }}
							</div>
							<div class="text-base text-ink-gray-5">is</div>
							<div class="flex-1">
								<FormControl
									v-if="def.type === 'select'"
									type="select"
									class="!min-w-[140px]"
									:options="def.options"
									:modelValue="filters[def.key] == null ? '' : filters[def.key]"
									@update:modelValue="(v) => setValue(def, v)"
								/>
								<div
									v-else-if="def.type === 'daterange'"
									class="flex items-center gap-1"
								>
									<DatePicker
										class="!min-w-[110px]"
										placeholder="From"
										:modelValue="filters.from_date || ''"
										@update:modelValue="(v) => setDate('from_date', v)"
									/>
									<DatePicker
										class="!min-w-[110px]"
										placeholder="To"
										:modelValue="filters.to_date || ''"
										@update:modelValue="(v) => setDate('to_date', v)"
									/>
								</div>
							</div>
							<Button variant="ghost" icon="x" @click="removeDef(def)" />
						</div>
					</div>
					<div class="flex items-center justify-between gap-2">
						<FormControl
							v-if="unsetDefs.length"
							type="select"
							variant="ghost"
							class="!text-ink-gray-5"
							:options="addOptions"
							:modelValue="''"
							@update:modelValue="addFilter"
						/>
						<div v-else />
						<Button
							v-if="activeCount || activeDefs.length"
							variant="ghost"
							label="Clear All Filters"
							class="!text-ink-gray-5"
							@click="clearAll"
						/>
					</div>
				</div>
			</template>
		</Popover>
		<Button
			v-if="activeCount"
			icon="x"
			class="rounded-l-none border-l"
			:tooltip="'Clear all filters'"
			@click="clearAll"
		/>
	</div>
</template>

<script setup>
// FilterButton - fixed per-page filter popover (DESIGN-V3 §5.3, D14):
// equals-selects + one date-range only; emits a plain {key: value} object
// (daterange contributes from_date/to_date keys). Count-chip split trigger,
// CRM popover anatomy ("Where/And <field> is <control>").
import { ref, computed } from "vue";
import { Popover, Button, FormControl, DatePicker } from "frappe-ui";

const props = defineProps({
	filterDefs: { type: Array, default: () => [] }, // [{key,label,type:'select'|'daterange',options}]
	filters: { type: Object, default: () => ({}) },
});

const emit = defineEmits(["update:filters"]);

// defs added via "+ Add Filter" but not yet holding a value
const pending = ref([]);

function hasValue(def) {
	if (def.type === "daterange") return !!(props.filters.from_date || props.filters.to_date);
	const v = props.filters[def.key];
	return v != null && v !== "";
}

const activeDefs = computed(() =>
	(props.filterDefs || []).filter((d) => hasValue(d) || pending.value.includes(d.key))
);
const activeCount = computed(() => (props.filterDefs || []).filter(hasValue).length);
const unsetDefs = computed(() =>
	(props.filterDefs || []).filter((d) => !hasValue(d) && !pending.value.includes(d.key))
);
const addOptions = computed(() => [
	{ label: "+ Add Filter", value: "" },
	...unsetDefs.value.map((d) => ({ label: d.label, value: d.key })),
]);

function setValue(def, value) {
	const next = { ...props.filters };
	if (value === "" || value == null) {
		delete next[def.key];
		if (!pending.value.includes(def.key)) pending.value = [...pending.value, def.key];
	} else {
		next[def.key] = value;
		pending.value = pending.value.filter((k) => k !== def.key);
	}
	emit("update:filters", next);
}

function setDate(key, value) {
	const next = { ...props.filters };
	if (!value) delete next[key];
	else next[key] = value;
	emit("update:filters", next);
}

function removeDef(def) {
	pending.value = pending.value.filter((k) => k !== def.key);
	const next = { ...props.filters };
	if (def.type === "daterange") {
		delete next.from_date;
		delete next.to_date;
	} else {
		delete next[def.key];
	}
	emit("update:filters", next);
}

function addFilter(key) {
	if (!key) return;
	if (!pending.value.includes(key)) pending.value = [...pending.value, key];
}

function clearAll() {
	pending.value = [];
	const next = { ...props.filters };
	for (const def of props.filterDefs || []) {
		if (def.type === "daterange") {
			delete next.from_date;
			delete next.to_date;
		} else {
			delete next[def.key];
		}
	}
	emit("update:filters", next);
}
</script>
