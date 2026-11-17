<template>
	<!-- Rows with no click of their own. frappe-ui's ListRow always wraps its cells in a
	     <button> (or an <a> with a row route), which nests the cells' own links and buttons
	     inside another control. Here the row is a plain div, so a link or button inside a
	     cell is the only control. Same grid and spacing as ListRow. -->
	<div
		v-for="(row, r) in list.rows"
		:key="row[list.rowKey]"
		class="flex flex-col rounded transition-all duration-300 ease-in-out"
		data-testid="plain-row"
	>
		<div
			class="grid items-center gap-4 px-2"
			:style="{
				height: rowHeight,
				gridTemplateColumns: getGridTemplateColumns(list.columns),
			}"
		>
			<div
				v-for="(column, i) in list.columns"
				:key="column.key"
				:class="[
					alignmentMap[column.align],
					i == 0 ? 'text-ink-gray-9' : 'text-ink-gray-7',
					'overflow-x-hidden',
				]"
			>
				<component
					:is="list.slots.cell"
					v-if="list.slots.cell"
					:column="column"
					:row="row"
					:item="row[column.key]"
					:align="column.align"
				/>
				<ListRowItem
					v-else
					:column="column"
					:row="row"
					:item="row[column.key]"
					:align="column.align"
				/>
			</div>
		</div>
		<div v-if="r < list.rows.length - 1" class="mx-2 h-px border-t border-outline-gray-1" />
	</div>
</template>

<script setup>
import { computed, inject } from "vue";
import { ListRowItem } from "frappe-ui";

// frappe-ui's ListView/utils.js is not exported by the package, so the two helpers are copied.
const alignmentMap = {
	left: "justify-start",
	start: "justify-start",
	center: "justify-center",
	middle: "justify-center",
	right: "justify-end",
	end: "justify-end",
};
function getGridTemplateColumns(columns) {
	return columns
		.map((c) => (typeof c.width === "number" ? c.width + "fr" : c.width || "1fr"))
		.join(" ");
}

// ListView provides its state as a computed under the key "list".
const injected = inject("list");
const list = computed(() => injected.value);
const rowHeight = computed(() => {
	const h = list.value.options.rowHeight;
	return typeof h === "number" ? `${h}px` : h;
});
</script>
