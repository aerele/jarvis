<template>
	<!-- Check → Yes/No mapped to 1/0 (plan §5.1) -->
	<PanelSelect
		v-if="rendered === 'check'"
		:aria-label="ariaLabel"
		:options="YES_NO"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- Select → metadata options. A LEADING BLANK is a real option meaning
	     'not set', so it is labelled rather than dropped, and the "no filter"
	     choice is removing the row - not picking an empty line. -->
	<PanelSelect
		v-else-if="rendered === 'select'"
		:aria-label="ariaLabel"
		:options="selectChoices"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- is set / is not set -->
	<PanelSelect
		v-else-if="rendered === 'is'"
		:aria-label="ariaLabel"
		:options="IS_OPTIONS"
		:modelValue="scalar || 'set'"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<PanelSelect
		v-else-if="rendered === 'timespan'"
		:aria-label="ariaLabel"
		:options="timespanChoices"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>

	<!-- Between: BOTH bounds are required. The server rejects a half-open range
	     rather than silently substituting today (deviation D14-a), so the row
	     stays pending here until both are set and is never sent half-filled. -->
	<div v-else-if="rendered === 'between-date'" class="flex items-center gap-1">
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
	<div v-else-if="rendered === 'between-datetime'" class="flex items-center gap-1">
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
		v-else-if="rendered === 'date'"
		:aria-label="ariaLabel"
		placeholder="Pick a date"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>
	<FormControl
		v-else-if="rendered === 'datetime'"
		type="datetime-local"
		:aria-label="ariaLabel"
		:modelValue="toDatetimeInput(scalar)"
		@update:modelValue="(v) => emitValue(fromDatetimeInput(v))"
	/>
	<FormControl
		v-else-if="rendered === 'time'"
		type="time"
		:aria-label="ariaLabel"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v)"
	/>
	<FormControl
		v-else-if="rendered === 'number'"
		type="number"
		:aria-label="ariaLabel"
		placeholder="Value"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v, { immediate: false })"
	/>

	<!-- Link: permission-aware search, titles shown, NAMES submitted (plan §5.3).
	     Backed by frappe.desk.search.search_link, the same endpoint the chat
	     mentions and the trigger DocType picker already use.
	     role=group + aria-label: frappe-ui's Autocomplete owns its own trigger
	     element, so the name goes on a labelled group around it rather than on a
	     node the component may re-render. Same string as the 16 plain controls. -->
	<div
		v-else-if="rendered === 'link' || rendered === 'multi-link'"
		role="group"
		:aria-label="ariaLabel"
	>
		<Autocomplete
			:multiple="rendered === 'multi-link'"
			:options="linkOptions"
			:loading="linkLoading"
			:modelValue="linkModel"
			:placeholder="`Search ${linkTargetName}…`"
			bodyClasses="min-w-[16rem]"
			@update:query="onLinkQuery"
			@update:modelValue="onLinkPick"
		/>
	</div>

	<!-- The picker cannot help right now: no searchable target (Dynamic Link
	     before its controlling field is read), the caller may not search that
	     DocType, or the last query matched nothing. The filter is still perfectly
	     usable — its value IS the document name — so type it. Typing here keeps
	     searching, so a typo is a detour and not a dead end: the moment a query
	     matches, the picker comes back. -->
	<div v-else-if="rendered === 'link-text'" class="flex flex-col gap-1">
		<FormControl
			type="text"
			:aria-label="ariaLabel"
			placeholder="Enter the exact name"
			:modelValue="scalar"
			@update:modelValue="onLinkText"
		/>
		<span v-if="linkFallbackReason" class="text-p-xs text-ink-gray-5">
			{{ linkFallbackReason }}
		</span>
	</div>

	<div
		v-else-if="rendered === 'multi-select'"
		class="flex flex-col gap-1"
		role="group"
		:aria-label="ariaLabel"
	>
		<Autocomplete
			:multiple="true"
			:options="selectChoices"
			:modelValue="listModel"
			placeholder="Values"
			bodyClasses="min-w-[16rem]"
			@update:modelValue="onListPick"
		/>
		<span v-if="limitNote" class="text-p-xs text-ink-amber-3">{{ limitNote }}</span>
	</div>

	<!-- in / not in over a free-text family: real chips, never a comma string
	     the server has to split (deviation D10). -->
	<div v-else-if="rendered === 'multi'" class="flex flex-col gap-1">
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
			@update:modelValue="onDraft"
			@keydown="onChipKey"
			@blur="commitChip"
		/>
		<span v-if="chipNote" class="text-p-xs text-ink-amber-3">{{ chipNote }}</span>
	</div>

	<!-- Data / text / Dynamic Link / anything else stored as a string.
	     No :debounce here: the wait belongs to the clause model (useListPage),
	     which covers every family including the ones whose control has no
	     debounce prop at all. -->
	<FormControl
		v-else
		type="text"
		:aria-label="ariaLabel"
		:placeholder="textPlaceholder"
		:modelValue="scalar"
		@update:modelValue="(v) => emitValue(v, { immediate: false })"
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
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { FormControl, Autocomplete, DatePicker, Badge, FeatherIcon } from "frappe-ui";
import PanelSelect from "@/components/list/PanelSelect.vue";
import {
	controlFor,
	selectControlOptions,
	linkTarget,
	timespanLabel,
	toDatetimeInput,
	fromDatetimeInput,
	TIMESPANS,
} from "@/components/list/filterModel";
import { searchLink } from "@/api";

const props = defineProps({
	entry: { type: Object, default: null }, // schema field entry
	clause: { type: Object, required: true },
	// What a screen reader calls this row; carries a positional cue when the
	// same field is filtered twice.
	rowName: { type: String, default: "" },
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

/**
 * What actually renders. Identical to `control` except when the Link picker has
 * nothing to offer, where a single Link degrades to a name input and a multi
 * Link degrades to CHIPS — a list has to stay a list, or an `in` clause would
 * come back as a bare string.
 */
const rendered = computed(() => {
	const c = control.value;
	if ((c === "link" || c === "multi-link") && linkFallback.value) {
		return c === "multi-link" ? "multi" : "link-text";
	}
	return c;
});
const ariaLabel = computed(
	() =>
		`Value for ${
			props.rowName || (props.entry && props.entry.label) || props.clause.fieldname
		}`
);
const selectChoices = computed(() => selectControlOptions(props.entry));
// Sentence case for reading; the VALUE stays the exact token the server's
// get_timespan_date_range matches on.
const timespanChoices = computed(() =>
	TIMESPANS.map((t) => ({ label: timespanLabel(t), value: t }))
);

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

// `immediate` distinguishes a decision from a keystroke: a picked option, a
// chosen date or a removed chip commits now; a character typed into a text or
// number box waits for the clause model's debounce.
function emitValue(value, opts) {
	emit("update:value", {
		value: value == null ? "" : value,
		display: null,
		immediate: !opts || opts.immediate !== false,
	});
}
function emitPair(i, value) {
	const next = pair.value.slice();
	next[i] = value == null ? "" : value;
	emit("update:value", { value: next, immediate: true });
}
function emitList(next) {
	const capped = next.slice(0, props.maxValues);
	limitNote.value =
		next.length > capped.length
			? `Limit reached. Only the first ${props.maxValues} values are used.`
			: "";
	emit("update:value", { value: capped, immediate: true });
}
// Uniform truncation feedback for every multi-value family, so a silently
// dropped value cannot look like one that was accepted.
const limitNote = ref("");

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
function onDraft(v) {
	draft.value = v;
	chipNote.value = "";
	// A multi-Link that fell back to chips is still a Link: keep searching so a
	// corrected query restores the picker.
	if (linkTargetName.value) onLinkQuery(v);
}

/** The single-Link fallback: the value IS the name, and it is also a query. */
function onLinkText(v) {
	emitValue(v, { immediate: false });
	onLinkQuery(v);
}
function commitChip() {
	const text = String(draft.value || "").trim();
	if (!text) {
		draft.value = "";
		return;
	}
	// Both refusals KEEP the typed text. Clearing it looked exactly like a
	// successful add whose chip failed to render, and the user retypes the same
	// value and watches it vanish again.
	if (list.value.length >= props.maxValues) {
		chipNote.value = `Limit reached. ${props.maxValues} values is the maximum.`;
		return;
	}
	if (list.value.includes(text)) {
		chipNote.value = `"${text}" is already in this filter.`;
		return;
	}
	draft.value = "";
	chipNote.value = "";
	emitList([...list.value, text]);
}
const chipNote = ref("");
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
// Why the picker gave up, or "" while it is still useful.
const linkFallbackReason = ref("");
const linkFallback = computed(() => !linkTargetName.value || !!linkFallbackReason.value);
let linkTimer = null;
let linkSeq = 0;

function onLinkQuery(query) {
	clearTimeout(linkTimer);
	linkTimer = setTimeout(() => loadLinks(query || ""), 300);
}

// The picker opens with the first page already in it. Without this the dropdown
// is empty until the user types, which reads as "there is nothing here" on a
// DocType they were about to browse. Rows only mount when the panel opens, so
// this costs one search per Link row per panel open.
onMounted(() => {
	if (control.value === "link" || control.value === "multi-link") loadLinks("");
});

async function loadLinks(query) {
	const doctype = linkTargetName.value;
	if (!doctype) return;
	const seq = ++linkSeq;
	linkLoading.value = true;
	try {
		// P1-06: pass WHERE this Link is used (its schema entry's own doctype +
		// fieldname) so a custom search_link hook / user-permission rule can key
		// off it. Untrusted context — the server still enforces the target's read
		// permission — and the values come from the shared schema, not the client.
		const rows = await searchLink(
			doctype,
			query,
			LINK_PAGE_LENGTH,
			props.entry && props.entry.doctype,
			props.entry && props.entry.fieldname
		);
		if (seq !== linkSeq) return; // stale - a newer keystroke superseded this
		linkOptions.value = (rows || []).map((r) => ({
			// Title when the DocType has one, name otherwise; the VALUE is always
			// the stable document name, which is what the filter submits.
			label: r.label || r.value,
			value: String(r.value),
			description: r.label && r.label !== r.value ? String(r.value) : r.description || "",
		}));
		if (linkOptions.value.length) {
			// Rows came back, so the picker is useful again. Clearing HERE rather
			// than on entry to loadLinks means the control never flickers while a
			// query is in flight — and it means a typo downgrades the row only for
			// as long as the typo lasts.
			linkFallbackReason.value = "";
		} else if (String(query || "").trim()) {
			// Nothing came back for something the user actually typed: the picker
			// has no more to offer, so hand them the input that can still express
			// the filter rather than an empty dropdown.
			linkFallbackReason.value = `No ${doctype} matched. Enter the name directly.`;
		}
	} catch (e) {
		// A caller who may not search this DocType gets no suggestions; the clause
		// is still usable by typing a name, so fall back instead of failing.
		if (seq === linkSeq) {
			linkOptions.value = [];
			linkFallbackReason.value = `Can't search ${doctype}. Enter the name directly.`;
		}
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
		const picked = (option || []).map((o) =>
			o && o.value != null ? String(o.value) : String(o)
		);
		emitList(picked);
		return;
	}
	if (!option) {
		emit("update:value", { value: "", display: null, immediate: true });
		return;
	}
	const value = option.value != null ? String(option.value) : "";
	emit("update:value", {
		value,
		display: option.label && option.label !== value ? option.label : null,
		immediate: true,
	});
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
		linkFallbackReason.value = "";
		draft.value = "";
		chipNote.value = "";
		limitNote.value = "";
		clearTimeout(linkTimer);
		if (control.value === "link" || control.value === "multi-link") loadLinks("");
	}
);
onBeforeUnmount(() => clearTimeout(linkTimer));
</script>
