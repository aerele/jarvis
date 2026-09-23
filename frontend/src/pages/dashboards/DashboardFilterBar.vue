<template>
	<div
		v-if="defs.length"
		class="flex flex-wrap items-end gap-3 px-5 pt-3"
		role="group"
		aria-label="Dashboard filters"
	>
		<!-- Builder-defined filters, shown above the canvas so a Connected
		     dashboard's data is scoped before it renders. Lives OUTSIDE the
		     sandboxed canvas: exports (PNG/PDF) never capture it. Labels only,
		     no heading (less-is-more). -->
		<div v-for="d in defs" :key="d.fieldname" class="flex min-w-[200px] flex-col gap-1">
			<span class="text-p-xs text-ink-gray-6">
				{{ d.label }}<span v-if="d.reqd" data-reqd class="text-ink-red-4"> *</span>
			</span>
			<div
				:data-error="errors[d.fieldname] ? '' : null"
				:class="errors[d.fieldname] ? 'rounded ring-1 ring-outline-red-2' : ''"
				role="group"
				:aria-label="`Value for ${d.label}`"
			>
				<Autocomplete
					:options="optionsFor(d)"
					:loading="loadingFor(d)"
					:modelValue="modelFor(d)"
					:placeholder="`Search ${d.options}…`"
					bodyClasses="min-w-[16rem]"
					@update:query="(q) => onQuery(d, q)"
					@update:modelValue="(opt) => pick(d.fieldname, opt)"
				/>
			</div>
		</div>
	</div>
</template>

<script setup>
// DashboardFilterBar - builder-defined Link filters, shown above the canvas
// on both the saved-dashboard view and the builder (Task 5). Copies
// FilterValueControl's Link branch: frappe-ui Autocomplete fed by
// useLinkSearch (debounced, fenced, primed on mount), one instance per
// definition since the bar renders an arbitrary list of fields inline rather
// than one control per mounted component.
import { watch, onBeforeUnmount } from "vue";
import { Autocomplete } from "frappe-ui";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";

const props = defineProps({
	defs: { type: Array, default: () => [] }, // [{fieldname,label,fieldtype,options,default,reqd}]
	modelValue: { type: Object, default: () => ({}) }, // {fieldname: value}
	errors: { type: Object, default: () => ({}) }, // {fieldname: true} for required-empty
});
const emit = defineEmits(["update:modelValue"]);

// fieldname -> { linkSearch, label, target }. `label` is the last-picked
// title, kept here (not derivable from the value alone) so the Autocomplete
// can show it without a round trip back to the server. `target` is the
// DocType (def.options) this instance currently searches - read by the
// fetcher AT CALL TIME (not captured once at creation) so a def that changes
// its `options` under the same fieldname (a builder html re-parse can
// re-point a filter at another DocType) searches the new target instead of
// silently reusing the old one; the defs watcher below is what actually
// updates `target` and reprimes when that happens.
const searches = new Map();

function ensure(d) {
	let entry = searches.get(d.fieldname);
	if (!entry) {
		entry = { linkSearch: null, label: "", target: d.options };
		entry.linkSearch = useLinkSearch((query) => searchLink(entry.target, query, 10), {
			mapper: (rows) =>
				(rows || []).map((r) => ({
					value: String(r.value),
					label: r.label || r.value,
					description:
						r.label && r.label !== r.value ? String(r.value) : r.description || "",
				})),
		});
		searches.set(d.fieldname, entry);
	}
	return entry;
}

function optionsFor(d) {
	return ensure(d).linkSearch.options.value;
}
function loadingFor(d) {
	return ensure(d).linkSearch.loading.value;
}
function modelFor(d) {
	const v = props.modelValue[d.fieldname] || "";
	if (!v) return null;
	return { value: v, label: ensure(d).label || v };
}
function onQuery(d, q) {
	ensure(d).linkSearch.onQuery(q);
}
function pick(name, opt) {
	const v = opt && opt.value != null ? String(opt.value) : "";
	const entry = searches.get(name);
	if (entry) entry.label = (opt && opt.label) || "";
	emit("update:modelValue", { ...props.modelValue, [name]: v });
}
defineExpose({ pick });

// Prime each field's first page on mount and whenever the definition set
// changes (a new html build declares different filters), drop the search
// state for any field that disappeared, and reprime a field whose `options`
// (link target DocType) changed under the SAME fieldname - the old value and
// any cached options/label belong to a DocType this field no longer points
// at, so both are cleared (FilterValueControl's field/operator-switch watch
// does the same reprime for the same reason).
watch(
	() => props.defs,
	(defs) => {
		const names = new Set();
		// Collected instead of emitted per-field: props.modelValue does not
		// update within this synchronous callback, so two retargeted fields in
		// the SAME defs change would each emit `{...props.modelValue, ...}`
		// off the same stale snapshot - a parent that replaces its state
		// wholesale (the normal v-model shape) would keep only the LAST emit,
		// silently un-clearing every earlier field. One emit at the end,
		// merging every cleared fieldname over the same base, avoids that.
		const cleared = [];
		for (const d of defs || []) {
			names.add(d.fieldname);
			const entry = ensure(d);
			if (entry.target !== d.options) {
				entry.target = d.options;
				entry.label = "";
				entry.linkSearch.reprime();
				cleared.push(d.fieldname);
			}
			entry.linkSearch.prime();
		}
		for (const [name, entry] of searches) {
			if (!names.has(name)) {
				entry.linkSearch.cleanup();
				searches.delete(name);
			}
		}
		if (cleared.length) {
			emit("update:modelValue", {
				...props.modelValue,
				...Object.fromEntries(cleared.map((k) => [k, ""])),
			});
		}
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	for (const entry of searches.values()) entry.linkSearch.cleanup();
});
</script>
