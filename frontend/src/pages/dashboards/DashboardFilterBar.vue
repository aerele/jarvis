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

// fieldname -> { linkSearch, label }. `label` is the last-picked title, kept
// here (not derivable from the value alone) so the Autocomplete can show it
// without a round trip back to the server.
const searches = new Map();

function ensure(d) {
	let entry = searches.get(d.fieldname);
	if (!entry) {
		const linkSearch = useLinkSearch((query) => searchLink(d.options, query, 10), {
			mapper: (rows) =>
				(rows || []).map((r) => ({
					value: String(r.value),
					label: r.label || r.value,
					description:
						r.label && r.label !== r.value ? String(r.value) : r.description || "",
				})),
		});
		entry = { linkSearch, label: "" };
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
// changes (a new html build declares different filters), and drop the search
// state for any field that disappeared.
watch(
	() => props.defs,
	(defs) => {
		const names = new Set();
		for (const d of defs || []) {
			names.add(d.fieldname);
			ensure(d).linkSearch.prime();
		}
		for (const [name, entry] of searches) {
			if (!names.has(name)) {
				entry.linkSearch.cleanup();
				searches.delete(name);
			}
		}
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	for (const entry of searches.values()) entry.linkSearch.cleanup();
});
</script>
