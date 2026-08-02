<template>
	<!-- Check → Yes/No mapped to 1/0 (plan §5.1) -->
	<FormControl
		v-if="control === 'check'"
		type="select"
		:aria-label="ariaLabel"
		:options="YES_NO"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- Select → metadata options. A LEADING BLANK is a real option meaning
	     'not set', so it is labelled rather than dropped, and the "no filter"
	     choice is removing the row - not picking an empty line. -->
	<FormControl
		v-else-if="control === 'select'"
		type="select"
		:aria-label="ariaLabel"
		:options="selectChoices"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- is set / is not set -->
	<FormControl
		v-else-if="control === 'is'"
		type="select"
		:aria-label="ariaLabel"
		:options="IS_OPTIONS"
		:modelValue="scalar || 'set'"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<FormControl
		v-else-if="control === 'timespan'"
		type="select"
		:aria-label="ariaLabel"
		:options="timespanChoices"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- Between: BOTH bounds are required. The server rejects a half-open range
	     rather than silently substituting today (deviation D14-a), so the row
	     stays pending here until both are set and is never sent half-filled. -->
	<div v-else-if="control === 'between-date'" class="flex items-center gap-1">
		<DatePicker
			class="!min-w-[110px]"
			placeholder="From"
			:aria-label="`${ariaLabel} from`"
			:modelValue="pair[0]"
			@update:modelValue="(v) => emitPair(0, v)"
		/>
		<DatePicker
			class="!min-w-[110px]"
			placeholder="To"
			:aria-label="`${ariaLabel} to`"
			:modelValue="pair[1]"
			@update:modelValue="(v) => emitPair(1, v)"
		/>
	</div>
	<div v-else-if="control === 'between-datetime'" class="flex items-center gap-1">
		<FormControl
			type="datetime-local"
			class="!min-w-[130px]"
			:aria-label="`${ariaLabel} from`"
			:modelValue="toDatetimeInput(pair[0])"
			@update:modelValue="(v) => emitPair(0, fromDatetimeInput(v))"
		/>
		<FormControl
			type="datetime-local"
			class="!min-w-[130px]"
			:aria-label="`${ariaLabel} to`"
			:modelValue="toDatetimeInput(pair[1])"
			@update:modelValue="(v) => emitPair(1, fromDatetimeInput(v))"
		/>
	</div>

	<DatePicker
		v-else-if="control === 'date'"
		:aria-label="ariaLabel"
		placeholder="Pick a date"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>
	<FormControl
		v-else-if="control === 'datetime'"
		type="datetime-local"
		:aria-label="ariaLabel"
		:modelValue="toDatetimeInput(scalar)"
		@update:modelValue="(v) => emitValue(fromDatetimeInput(v))"
	/>
	<FormControl
		v-else-if="control === 'time'"
		type="time"
		:aria-label="ariaLabel"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>
	<FormControl
		v-else-if="control === 'number'"
		type="number"
		:aria-label="ariaLabel"
		placeholder="Value"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- Link: permission-aware search, titles shown, NAMES submitted (plan §5.3).
	     Backed by frappe.desk.search.search_link, the same endpoint the chat
	     mentions and the trigger DocType picker already use. -->
	<Autocomplete
		v-else-if="control === 'link' || control === 'multi-link'"
		:multiple="control === 'multi-link'"
		:options="linkOptions"
		:loading="linkLoading"
		:modelValue="linkModel"
		:placeholder="linkTargetName ? `Search ${linkTargetName}…` : 'Value'"
		bodyClasses="min-w-[16rem]"
		@update:query="onLinkQuery"
		@update:modelValue="onLinkPick"
	/>

	<Autocomplete
		v-else-if="control === 'multi-select'"
		:multiple="true"
		:options="selectChoices"
		:modelValue="listModel"
		placeholder="Values"
		bodyClasses="min-w-[16rem]"
		@update:modelValue="onListPick"
	/>

	<!-- in / not in over a free-text family: real chips, never a comma string
	     the server has to split (deviation D10). -->
	<div v-else-if="control === 'multi'" class="flex flex-col gap-1">
		<div v-if="list.length" class="flex flex-wrap items-center gap-1">
			<Badge v-for="(v, i) in list" :key="`${v}-${i}`" variant="subtle" theme="gray">
				<span class="max-w-[10rem] truncate">{{ v }}</span>
				<template #suffix>
					<button
						type="button"
						class="ml-1 text-ink-gray-5 hover:text-ink-gray-8"
						:aria-label="`Remove ${v}`"
						@click="removeChip(i)"
					>
						<FeatherIcon name="x" class="size-3" />
					</button>
				</template>
			</Badge>
		</div>
		<FormControl
			type="text"
			:aria-label="ariaLabel"
			:placeholder="chipPlaceholder"
			:modelValue="draft"
			@update:modelValue="(v) => (draft = v)"
			@keydown="onChipKey"
			@blur="commitChip"
		/>
	</div>

	<!-- Data / text / Dynamic Link / anything else stored as a string -->
	<FormControl
		v-else
		type="text"
		:aria-label="ariaLabel"
		:placeholder="textPlaceholder"
		:modelValue="scalar"
		:debounce="300"
		@update:modelValue="(v) => emitValue(v)"
	/>
</template>

<script setup>
// FilterValueControl - one clause's VALUE, rendered for its field family.
//
// Split out of FilterGroup so the family matrix (Check/Select/Link/date/
// datetime-range/numeric/multi-value/text) is one testable unit instead of a
// hundred lines of v-if inside the panel. It owns no clause state: it reads the
// clause + its schema entry and emits a patch ({value} and, for Link, the
// {display} title we should keep showing).
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { FormControl, Autocomplete, DatePicker, Badge, FeatherIcon } from "frappe-ui";
import {
	controlFor,
	selectControlOptions,
	linkTarget,
	toDatetimeInput,
	fromDatetimeInput,
	TIMESPANS,
} from "@/components/list/filterModel";
import { searchLink } from "@/api";

const props = defineProps({
	entry: { type: Object, default: null }, // schema field entry
	clause: { type: Object, required: true },
	maxValues: { type: Number, default: 100 },
});
const emit = defineEmits(["update:value"]);

const YES_NO = [
	{ label: "Yes", value: "1" },
	{ label: "No", value: "0" },
];
const IS_OPTIONS = [
	{ label: "Set", value: "set" },
	{ label: "Not set", value: "not set" },
];

const control = computed(() => controlFor(props.entry, props.clause.operator));
const ariaLabel = computed(
	() => `Value for ${(props.entry && props.entry.label) || props.clause.fieldname}`
);
const selectChoices = computed(() => selectControlOptions(props.entry));
const timespanChoices = computed(() => TIMESPANS.map((t) => ({ label: t, value: t })));

const scalar = computed(() => {
	const v = props.clause.value;
	return v === null || v === undefined || Array.isArray(v) ? "" : String(v);
});
const pair = computed(() => {
	const v = props.clause.value;
	return Array.isArray(v) ? [v[0] || "", v[1] || ""] : ["", ""];
});
const list = computed(() => {
	const v = props.clause.value;
	return Array.isArray(v) ? v.map((x) => String(x)) : [];
});

const textPlaceholder = computed(() => {
	const op = props.clause.operator;
	// D13: a `like` CLAUSE is the query language - the wildcards the user types
	// stay wildcards, and the term is wrapped when they type none. Say so.
	if (op === "like" || op === "not like") return "Contains… (use % to anchor)";
	return "Value";
});
const chipPlaceholder = computed(() =>
	list.value.length >= props.maxValues ? "Limit reached" : "Type a value, press Enter"
);

function emitValue(value) {
	emit("update:value", { value: value == null ? "" : value, display: null });
}
function emitPair(i, value) {
	const next = pair.value.slice();
	next[i] = value == null ? "" : value;
	emit("update:value", { value: next });
}
function emitList(next) {
	emit("update:value", { value: next.slice(0, props.maxValues) });
}

// ── chips (free-text in / not in) ───────────────────────────────────────────
const draft = ref("");
// Enter and comma both commit, and Backspace on an empty draft removes the last
// chip - the keyboard conventions of every chip input, so the control is usable
// without a mouse (plan §6.4's accessibility requirement).
function onChipKey(event) {
	if (event.key === "Enter" || event.key === ",") {
		event.preventDefault();
		commitChip();
		return;
	}
	if (event.key === "Backspace" && !String(draft.value || "") && list.value.length) {
		event.preventDefault();
		removeChip(list.value.length - 1);
	}
}
function commitChip() {
	const text = String(draft.value || "").trim();
	draft.value = "";
	if (!text || list.value.length >= props.maxValues) return;
	if (list.value.includes(text)) return;
	emitList([...list.value, text]);
}
function removeChip(i) {
	const next = list.value.slice();
	next.splice(i, 1);
	emitList(next);
}

// ── Link search (debounced + fenced) ────────────────────────────────────────
// 300ms debounce and a monotonic sequence, exactly like the DocType picker in
// TriggerDetail: without the fence a slow "ab" response lands after "abcd" and
// the dropdown shows options for a query the user has already moved past.
const LINK_PAGE_LENGTH = 20; // plan §9's Link suggestion cap
const linkOptions = ref([]);
const linkLoading = ref(false);
const linkTargetName = computed(() => linkTarget(props.entry));
let linkTimer = null;
let linkSeq = 0;

function onLinkQuery(query) {
	clearTimeout(linkTimer);
	linkTimer = setTimeout(() => loadLinks(query || ""), 300);
}

async function loadLinks(query) {
	const doctype = linkTargetName.value;
	if (!doctype) return;
	const seq = ++linkSeq;
	linkLoading.value = true;
	try {
		const rows = await searchLink(doctype, query, LINK_PAGE_LENGTH);
		if (seq !== linkSeq) return; // stale - a newer keystroke superseded this
		linkOptions.value = (rows || []).map((r) => ({
			// Title when the DocType has one, name otherwise; the VALUE is always
			// the stable document name, which is what the filter submits.
			label: r.label || r.value,
			value: String(r.value),
			description: r.label && r.label !== r.value ? String(r.value) : r.description || "",
		}));
	} catch (e) {
		// A caller who cannot read the target DocType gets no suggestions; the
		// clause is still usable by typing a name, so this is not an error state.
		if (seq === linkSeq) linkOptions.value = [];
	} finally {
		if (seq === linkSeq) linkLoading.value = false;
	}
}

const linkModel = computed(() => {
	if (control.value === "multi-link") return listModel.value;
	const value = scalar.value;
	if (!value) return null;
	return { label: props.clause.display || value, value };
});
const listModel = computed(() => list.value.map((v) => ({ label: v, value: v })));

function onLinkPick(option) {
	if (control.value === "multi-link") {
		const picked = (option || []).map((o) => (o && o.value != null ? String(o.value) : String(o)));
		emitList(picked);
		return;
	}
	if (!option) {
		emit("update:value", { value: "", display: null });
		return;
	}
	const value = option.value != null ? String(option.value) : "";
	emit("update:value", { value, display: option.label && option.label !== value ? option.label : null });
}

function onListPick(options) {
	emitList((options || []).map((o) => (o && o.value != null ? String(o.value) : String(o))));
}

// Switching field or operator invalidates the suggestion list (it belonged to
// the previous DocType) and any half-typed chip.
watch(
	() => [props.clause.doctype, props.clause.fieldname, props.clause.operator].join("|"),
	() => {
		linkSeq += 1;
		linkOptions.value = [];
		linkLoading.value = false;
		draft.value = "";
		clearTimeout(linkTimer);
	}
);
onBeforeUnmount(() => clearTimeout(linkTimer));
</script>
