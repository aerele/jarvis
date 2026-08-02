<template>
	<div class="flex items-center">
		<Popover placement="bottom-end" @update:show="onShowChange">
			<template #target="{ togglePopover }">
				<Button
					ref="triggerEl"
					label="Filter"
					iconLeft="filter"
					:class="count ? 'rounded-r-none' : ''"
					:aria-label="count ? `Filter (${count} active)` : 'Filter'"
					@click="togglePopover()"
				>
					<template v-if="count" #suffix>
						<div
							class="flex h-5 w-5 items-center justify-center rounded-[5px] bg-surface-white pt-px text-xs font-medium text-ink-gray-8 shadow-sm"
						>
							{{ count }}
						</div>
					</template>
				</Button>
			</template>
			<template #body>
				<div
					class="my-2 w-[38rem] max-w-[calc(100vw-2rem)] rounded-lg bg-surface-modal p-2 shadow-2xl ring-1 ring-black ring-opacity-5"
					role="group"
					aria-label="Filters"
				>
					<!-- schema loading / unavailable: the LIST is unaffected, only the
					     field catalog is missing, so this is a retry and not an error page -->
					<div
						v-if="schemaState === 'loading'"
						class="flex h-16 items-center justify-center gap-2 text-base text-ink-gray-5"
					>
						<LoadingIndicator class="size-4" />
						<span>Loading fields…</span>
					</div>
					<div v-else-if="schemaState === 'error'" class="flex flex-col gap-3 p-3">
						<div class="flex items-start gap-2">
							<FeatherIcon name="alert-circle" class="mt-0.5 size-4 text-ink-red-4" />
							<div class="flex flex-col gap-1">
								<span class="text-base font-medium text-ink-gray-8">
									Filter fields couldn't be loaded
								</span>
								<span class="text-p-sm text-ink-gray-6">{{ schemaMessage }}</span>
							</div>
						</div>
						<div class="flex items-center gap-2">
							<Button label="Try again" @click="$emit('request-schema')" />
							<Button
								v-if="clauses.length"
								variant="ghost"
								label="Clear all filters"
								class="!text-ink-gray-5"
								@click="clearAll"
							/>
						</div>
					</div>

					<template v-else>
						<div
							v-if="!clauses.length"
							class="mb-3 flex h-7 items-center px-3 text-sm text-ink-gray-5"
						>
							Empty - Choose a field to filter by
						</div>
						<div v-else ref="rowsEl" class="mb-3 flex flex-col gap-2">
							<div
								v-for="(clause, i) in clauses"
								:key="clause.id"
								data-filter-row
								class="flex items-start gap-2"
							>
								<div
									class="w-13 shrink-0 pt-1 pl-2 text-end text-base text-ink-gray-5"
								>
									{{ i === 0 ? "Where" : "And" }}
								</div>
								<div class="min-w-0 flex-1">
									<div class="flex items-start gap-2">
										<!-- field -->
										<div class="min-w-0 flex-1">
											<Autocomplete
												:options="fieldGroups"
												:modelValue="fieldValue(clause)"
												:placeholder="'Field'"
												bodyClasses="min-w-[18rem]"
												@update:modelValue="(o) => pickField(i, o)"
											/>
										</div>
										<!-- operator -->
										<div class="w-32 shrink-0">
											<FormControl
												type="select"
												:aria-label="`Condition for ${rowName(i)}`"
												:options="operatorOptions(clause)"
												:modelValue="clause.operator"
												@update:modelValue="(v) => pickOperator(i, v)"
											/>
										</div>
										<!-- value -->
										<div class="min-w-0 flex-1">
											<FilterValueControl
												:entry="entryOf(clause)"
												:clause="clause"
												:row-name="rowName(i)"
												:max-values="limits.max_in_values"
												@update:value="(v) => setValue(i, v)"
											/>
										</div>
										<Button
											variant="ghost"
											icon="x"
											:aria-label="`Remove filter on ${rowName(i)}`"
											@click="removeAt(i)"
										/>
									</div>
									<ErrorMessage
										v-if="rowErrorId === clause.id"
										class="mt-1"
										:message="error.message"
									/>
									<div
										v-else-if="isPending(clause)"
										class="mt-1 text-p-xs text-ink-gray-5"
									>
										{{ pendingHint(clause) }}
									</div>
								</div>
							</div>
						</div>

						<!-- panel-level rejections that no single row owns -->
						<ErrorMessage v-if="panelError" class="mb-2 px-3" :message="panelError" />
						<div
							v-if="capReached"
							class="mb-2 mx-1 rounded-md p-2 ring-1 ring-outline-gray-modals text-xs text-ink-gray-7"
						>
							{{ limits.max_clauses }} filters is the maximum for one list.
						</div>

						<div class="flex items-center justify-between gap-2">
							<Autocomplete
								v-if="!capReached"
								:options="fieldGroups"
								:modelValue="null"
								placeholder="+ Add Filter"
								bodyClasses="min-w-[18rem]"
								@update:modelValue="addFilter"
							>
								<template #target="{ togglePopover }">
									<Button
										ref="addEl"
										variant="ghost"
										label="+ Add Filter"
										class="!text-ink-gray-5"
										@click="togglePopover()"
									/>
								</template>
							</Autocomplete>
							<div v-else />
							<Button
								v-if="clauses.length"
								variant="ghost"
								label="Clear All Filters"
								class="!text-ink-gray-5"
								@click="clearAll"
							/>
						</div>
					</template>
				</div>
			</template>
		</Popover>
		<Button
			v-if="count"
			icon="x"
			class="rounded-l-none border-l"
			:tooltip="'Clear all filters'"
			aria-label="Clear all filters"
			@click="clearAll({ toTrigger: true })"
		/>
	</div>
</template>

<script setup>
// FilterGroup - the shared Frappe-style Where/And filter panel (plan 08 §6.4).
//
// It replaces FilterButton on a MIGRATED surface only: FilterButton stays for
// every list that still owns a curated `filterDefs` array, and the two never
// render together. What changes here is the authority — the field picker is the
// server's per-caller schema (jarvis.chat.list_filters.get_list_filter_schema),
// not a page-authored list, so a custom field becomes filterable without a
// frontend release and a field the caller may not read is simply not there.
//
// The component is CONTROLLED: useListPage owns the clause array, the schema
// fetch, the URL and the request. This file renders rows, decides which value
// control a field family wants, and emits a new clause array. Anything it emits
// is re-validated server-side; the operator menu here is convenience, never a
// permission boundary.
import { computed, ref, watch, nextTick } from "vue";
import {
	Popover,
	Button,
	FormControl,
	Autocomplete,
	ErrorMessage,
	LoadingIndicator,
	FeatherIcon,
} from "frappe-ui";
import FilterValueControl from "@/components/list/FilterValueControl.vue";
import {
	schemaIndex,
	fieldOptions,
	fieldKey,
	entryFor,
	limitsOf,
	activeCount,
	isComplete,
	clauseForEntry,
	retargetClause,
	setOperator,
	operatorLabel,
	attributeError,
} from "@/components/list/filterModel";

const props = defineProps({
	schema: { type: Object, default: null },
	// "idle" (not fetched yet) | "loading" | "ready" | "error"
	schemaState: { type: String, default: "idle" },
	schemaError: { type: Object, default: null }, // {code, kind, message}
	clauses: { type: Array, default: () => [] },
	error: { type: Object, default: null }, // last request's filter rejection
});

const emit = defineEmits(["update:clauses", "request-schema"]);

const index = computed(() => schemaIndex(props.schema));
const fieldGroups = computed(() => fieldOptions(props.schema));
const limits = computed(() => limitsOf(props.schema));
const count = computed(() => activeCount(props.clauses, index.value));
const capReached = computed(() => props.clauses.length >= limits.value.max_clauses);

// The schema is fetched LAZILY - a list that nobody filters never pays for a
// metadata walk. (useListPage fetches eagerly instead when the URL arrives with
// clauses, because those must be reconciled before the first page is fetched.)
// Retry re-emits the same request: the parent flips schemaState to "loading",
// which is why this button has no loading state of its own.
//
// The signal is `update:show`, NOT `open`. frappe-ui's Popover emits `open`
// only from its reka-ui root, and it binds that root's `v-model:open` with a
// real boolean - so the root is controlled and reports only the changes IT
// starts (outside click, Escape). This panel opens from a Button inside the
// `#target` slot, which runs frappe-ui's own `isOpen` setter and emits
// `update:show` alone. Listening for `open` meant the catalog was never
// requested and the field picker was permanently empty.
//
// `update:show` fires on the way down as well as up, and reka relays one
// dismissal as two identical emissions, so only a real change of state counts.
let lastShown = false;
function onShowChange(shown) {
	if (shown === lastShown) return;
	lastShown = shown;
	if (shown && props.schemaState === "idle") emit("request-schema");
}

const schemaMessage = computed(
	() => (props.schemaError && props.schemaError.message) || "Please try again."
);

function entryOf(clause) {
	return entryFor(index.value, clause);
}
function labelOf(clause) {
	const entry = entryOf(clause);
	return (entry && entry.label) || clause.fieldname || "field";
}
function fieldValue(clause) {
	const entry = entryOf(clause);
	if (!entry) {
		// A clause whose field vanished between renders: name it honestly rather
		// than showing an empty picker that looks like the user lost their input.
		return { label: `${clause.fieldname} (unavailable)`, value: "" };
	}
	return { label: entry.label, value: fieldKey(entry.doctype, entry.fieldname) };
}
function operatorOptions(clause) {
	const entry = entryOf(clause);
	const operators = (entry && entry.operators) || [clause.operator];
	return operators.map((op) => ({
		label: operatorLabel(op, entry && entry.fieldtype),
		value: op,
	}));
}

/**
 * What a screen reader calls this row.
 *
 * The same field may legitimately appear more than once (that is the whole
 * point of clauses over a `{key: value}` object), and "Condition for Created On"
 * announced twice is two controls a non-sighted user cannot tell apart. Repeats
 * get a 1-based ordinal; a label that appears once is left clean.
 */
const rowNames = computed(() => {
	const counts = new Map();
	for (const clause of props.clauses) {
		const label = labelOf(clause);
		counts.set(label, (counts.get(label) || 0) + 1);
	}
	const seen = new Map();
	return props.clauses.map((clause) => {
		const label = labelOf(clause);
		if ((counts.get(label) || 0) < 2) return label;
		const n = (seen.get(label) || 0) + 1;
		seen.set(label, n);
		return `${label} (filter ${n})`;
	});
});
function rowName(i) {
	return rowNames.value[i] || "field";
}

function isPending(clause) {
	return !isComplete(clause, entryOf(clause));
}
function pendingHint(clause) {
	// The catalog check comes FIRST: a vanished field is why the row is pending,
	// and "needs a start and an end" would send someone off filling in a range
	// that can never apply.
	if (!entryOf(clause)) return "This field is no longer available - remove it.";
	if (clause.operator === "Between") return "Needs a start and an end to apply.";
	if (clause.operator === "in" || clause.operator === "not in")
		return "Add at least one value to apply.";
	return "Enter a value to apply.";
}

// ── error placement (never a dead-end toast) ────────────────────────────────
const rowErrorId = computed(() => attributeError(props.error, props.clauses, index.value));
const panelError = computed(() => {
	const error = props.error;
	if (!error) return "";
	if (error.kind === "cap") return error.message;
	if (error.kind === "row" && rowErrorId.value) return ""; // shown on the row
	return error.message;
});

// ── mutations (always emit a NEW array; the parent owns the state) ──────────
// `immediate: false` says "the user is still typing": useListPage applies the
// model at once but waits out its debounce before turning it into a request and
// a URL. Every other mutation is a discrete decision and commits now.
function replace(next, opts) {
	emit("update:clauses", next, opts || { immediate: true });
}

function lookup(option) {
	return option && index.value ? index.value.get(option.value) : null;
}

// ── focus management ───────────────────────────────────────────────────────
// Adding a row moves focus INTO it (its condition menu, the first thing worth
// changing); removing one moves focus to the row that took its place, or to
// "+ Add Filter" when the last row goes. Without this, every add and remove
// drops focus on <body> and a keyboard user restarts from the top of the page.
const rowsEl = ref(null);
const addEl = ref(null);
const triggerEl = ref(null);
// Where focus should land once the PARENT has applied the change. Focusing at
// mutation time would race the round trip through useListPage, so the intent is
// recorded here and spent when the row count actually moves.
let focusIntent = null;

watch(
	() => props.clauses.length,
	() => {
		if (focusIntent === null) return;
		const i = focusIntent;
		focusIntent = null;
		focusRow(i);
	}
);

// `ref` on a component hands back the instance, not the element.
function elementOf(handle) {
	const el = handle && (handle.$el || handle);
	return el && el.focus ? el : null;
}

function focusRow(i) {
	nextTick(() => {
		if (focusTarget === "trigger") {
			// The clear-X outside the popover: the popover is closed, so there is no
			// row to land on and "+ Add Filter" is not on screen either. Focus goes
			// back to the control the user just pressed next to.
			focusTarget = null;
			const trigger = elementOf(triggerEl.value);
			if (trigger) trigger.focus();
			return;
		}
		focusTarget = null;
		const rows = rowsEl.value ? rowsEl.value.querySelectorAll("[data-filter-row]") : [];
		const row = rows.length ? rows[Math.min(i, rows.length - 1)] : null;
		const control = row && row.querySelector("select, input, button");
		if (control && control.focus) {
			control.focus();
			return;
		}
		const add = elementOf(addEl.value);
		if (add) add.focus();
	});
}
let focusTarget = null;

function addFilter(option) {
	const entry = lookup(option);
	if (!entry || capReached.value) return;
	focusIntent = props.clauses.length;
	replace([...props.clauses, clauseForEntry(entry)]);
}
function pickField(i, option) {
	const entry = lookup(option);
	if (!entry) return;
	const next = props.clauses.slice();
	next[i] = retargetClause(next[i], entry);
	replace(next);
}
function pickOperator(i, operator) {
	if (!operator) return;
	const next = props.clauses.slice();
	next[i] = setOperator(next[i], operator);
	replace(next);
}
function setValue(i, patch) {
	const { immediate, ...value } = patch || {};
	const next = props.clauses.slice();
	next[i] = { ...next[i], ...value };
	replace(next, { immediate: immediate !== false });
}
function removeAt(i) {
	const next = props.clauses.slice();
	next.splice(i, 1);
	focusIntent = i;
	replace(next);
}
function clearAll(opts) {
	if (!props.clauses.length) return;
	// Clearing empties the row list, so focus has nowhere natural to go: from
	// inside the popover it lands on "+ Add Filter" (via focusRow's fallback),
	// from the outer split button it goes back to the Filter trigger.
	focusTarget = opts && opts.toTrigger ? "trigger" : null;
	focusIntent = 0;
	replace([]);
}
</script>
