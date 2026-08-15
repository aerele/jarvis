<template>
	<div class="wg-filter">
		<div class="wg-group">
			<span class="wg-lbl">Color</span>
			<button
				v-for="m in modes"
				:key="m.v"
				:class="['wg-btn', mode === m.v ? 'active' : '']"
				@click="$emit('update:mode', m.v)"
			>
				{{ m.t }}
			</button>
		</div>
		<div class="wg-group">
			<span class="wg-lbl">Layer</span>
			<button
				v-for="o in overlays"
				:key="o.v"
				:class="['wg-btn', overlay === o.v ? 'active' : '']"
				@click="$emit('update:overlay', o.v)"
			>
				{{ o.t }}
			</button>
		</div>
		<div class="wg-legend">
			<span v-for="l in legend.nodes" :key="l.label" class="wg-swatch">
				<i :style="{ background: l.color }"></i>{{ l.label }}
			</span>
		</div>
		<span class="wg-count text-muted" v-if="total"
			>showing {{ shown }} / {{ total }} pages</span
		>
	</div>
</template>

<script>
import { LEGEND } from "./graphStyle";
export default {
	name: "FilterBar",
	props: {
		mode: { type: String, default: "kind" },
		overlay: { type: String, default: "knowledge" },
		shown: { type: Number, default: 0 },
		total: { type: Number, default: 0 },
	},
	emits: ["update:mode", "update:overlay"],
	setup() {
		return {
			legend: LEGEND,
			modes: [
				{ v: "kind", t: "By kind" },
				{ v: "community", t: "By cluster" },
			],
			overlays: [
				{ v: "knowledge", t: "Knowledge" },
				{ v: "utilization", t: "+ Who" },
				{ v: "demand", t: "+ Demand" },
			],
		};
	},
};
</script>

<style scoped>
.wg-filter {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 14px;
	margin-bottom: 10px;
}
.wg-group {
	display: flex;
	align-items: center;
	gap: 4px;
}
.wg-lbl {
	font-size: 11px;
	text-transform: uppercase;
	color: var(--text-muted, #888);
	margin-right: 4px;
}
.wg-btn {
	font-size: 12px;
	padding: 3px 10px;
	border: 1px solid var(--border-color, #d1d8dd);
	border-radius: 6px;
	background: var(--card-bg, #fff);
	cursor: pointer;
}
.wg-btn.active {
	background: var(--bg-blue, #4c9aff);
	color: #fff;
	border-color: var(--bg-blue, #4c9aff);
}
.wg-legend {
	display: flex;
	flex-wrap: wrap;
	gap: 10px;
}
.wg-swatch {
	font-size: 11px;
	color: var(--text-muted, #777);
	display: inline-flex;
	align-items: center;
	gap: 4px;
}
.wg-swatch i {
	width: 10px;
	height: 10px;
	border-radius: 50%;
	display: inline-block;
}
.wg-count {
	font-size: 11px;
	margin-left: auto;
}
</style>
