<template>
	<div class="wg-excl" v-if="pageTypes.length > 1">
		<span class="wg-excl-l">Show:</span>
		<button
			v-for="t in pageTypes"
			:key="t"
			:class="['wg-chip', isOn(t) ? 'on' : 'off']"
			@click="toggle(t)"
		>
			{{ t }}
		</button>
	</div>
</template>

<script>
// Exclusion rules — click a page-type chip to hide it from the graph + analysis
// (they auto-refresh). Persisted by the surface (localStorage tenant / query-arg
// admin). Off chip = excluded.
export default {
	name: "ExclusionRules",
	props: {
		pageTypes: { type: Array, default: () => [] },
		excluded: { type: Array, default: () => [] },
	},
	emits: ["update:excluded"],
	methods: {
		isOn(t) {
			return !this.excluded.includes(t);
		},
		toggle(t) {
			const set = new Set(this.excluded);
			set.has(t) ? set.delete(t) : set.add(t);
			this.$emit("update:excluded", [...set]);
		},
	},
};
</script>

<style scoped>
.wg-excl {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 5px;
}
.wg-excl-l {
	font-size: 11px;
	text-transform: uppercase;
	color: var(--text-muted, #888);
	margin-right: 2px;
}
.wg-chip {
	font-size: 11px;
	padding: 2px 9px;
	border: 1px solid var(--border-color, #d1d8dd);
	border-radius: 12px;
	background: var(--card-bg, #fff);
	cursor: pointer;
}
.wg-chip.on {
	border-color: var(--bg-blue, #4c9aff);
	color: var(--bg-blue, #4c9aff);
}
.wg-chip.off {
	color: var(--text-muted, #aaa);
	text-decoration: line-through;
	opacity: 0.6;
}
</style>
