<template>
	<div class="wg-risk">
		<Sec title="Fix now (hot + stale)" :n="fixNow.length" empty="Nothing rotting" warn>
			<li v-for="(p, i) in fixNow.slice(0, 8)" :key="'f' + i">
				<a href="#" @click.prevent="$emit('pick', p.slug)">{{ p.label || p.slug }}</a>
				<span class="m">{{ p.demand }} reads · {{ p.reason }}</span>
			</li>
		</Sec>
		<Sec
			title="Bus-factor risk (single author)"
			:n="busFactor.length"
			empty="No single points of failure"
			warn
		>
			<li v-for="(p, i) in busFactor.slice(0, 8)" :key="'b' + i">
				<a href="#" @click.prevent="$emit('pick', p.slug)">{{ p.label || p.slug }}</a>
				<span class="m">{{ p.author }} · {{ p.demand }} reads</span>
			</li>
		</Sec>
		<Sec
			title="Safe to archive (stale + unread)"
			:n="archive.safe_to_archive.length"
			empty="Nothing to retire"
		>
			<li v-for="(p, i) in archive.safe_to_archive.slice(0, 8)" :key="'s' + i">
				<a href="#" @click.prevent="$emit('pick', p.slug)">{{ p.label || p.slug }}</a>
			</li>
		</Sec>
		<Sec
			title="Content gaps (searched, missing)"
			:n="gaps.missing.length"
			empty="No missing content"
			warn
		>
			<li v-for="(g, i) in gaps.missing.slice(0, 8)" :key="'g' + i">
				<span>{{ g.query_norm }}</span
				><span class="m">asked {{ g.asked }}×</span>
			</li>
		</Sec>
		<Sec
			title="Terminology gaps (thin page exists)"
			:n="gaps.terminology.length"
			empty="No thin pages hit"
		>
			<li v-for="(g, i) in gaps.terminology.slice(0, 6)" :key="'t' + i">
				<span>{{ g.query_norm }}</span
				><span class="m">asked {{ g.asked }}×</span>
			</li>
		</Sec>
	</div>
</template>

<script>
import { h } from "vue";
// runtime-only Vue can't compile template strings — render fn instead (#1)
const Sec = {
	name: "Sec",
	props: { title: String, n: Number, empty: String, warn: Boolean },
	render() {
		return h("div", { class: "wg-sec" }, [
			h("div", { class: ["wg-sec-h", { warn: this.warn }] }, [
				this.title + " ",
				h("span", { class: "wg-n" }, String(this.n)),
			]),
			this.n
				? h("ul", null, this.$slots.default ? this.$slots.default() : undefined)
				: h("div", { class: "wg-empty text-muted" }, h("i", null, this.empty)),
		]);
	},
};
export default {
	name: "RiskPanel",
	components: { Sec },
	props: {
		archive: { type: Object, default: () => ({ fix_now: [], safe_to_archive: [] }) },
		busFactor: { type: Array, default: () => [] },
		gaps: { type: Object, default: () => ({ missing: [], terminology: [] }) },
	},
	emits: ["pick"],
	computed: {
		fixNow() {
			return this.archive.fix_now || [];
		},
	},
};
</script>

<style scoped>
/* Sec's own markup is h()-rendered, not from this SFC's <template>, so it
   never gets the scoped data-v attribute — :deep() targets it by ancestry.
   .m lives in the default slot content (this SFC's own template), stays as-is. */
.wg-risk {
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 8px;
	padding: 12px;
}
:deep(.wg-sec) {
	margin-bottom: 12px;
}
:deep(.wg-sec:last-child) {
	margin-bottom: 0;
}
:deep(.wg-sec-h) {
	font-size: 11px;
	text-transform: uppercase;
	color: var(--text-muted, #888);
	margin-bottom: 4px;
}
:deep(.wg-sec-h.warn) {
	color: #d9534f;
}
:deep(.wg-sec-h .wg-n) {
	opacity: 0.6;
}
:deep(.wg-sec ul) {
	list-style: none;
	margin: 0;
	padding: 0;
}
:deep(.wg-sec li) {
	display: flex;
	justify-content: space-between;
	gap: 8px;
	font-size: 12px;
	padding: 2px 0;
}
.wg-sec .m {
	color: var(--text-muted, #999);
	white-space: nowrap;
	font-size: 11px;
}
:deep(.wg-empty) {
	font-size: 12px;
}
</style>
