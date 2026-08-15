<template>
	<div class="wg-act">
		<Sec
			title="Fix now (stale / contradicted)"
			:n="actions.stale.length"
			empty="Knowledge is fresh"
			warn
		>
			<li v-for="(p, i) in actions.stale.slice(0, 8)" :key="'s' + i">
				<a href="#" @click.prevent="$emit('pick', p.id)">{{ p.label || p.slug }}</a>
				<span class="m">{{ p.reason }}</span>
			</li>
		</Sec>
		<Sec
			title="Suggested links (connect these)"
			:n="actions.suggest.length"
			empty="No suggestions yet"
		>
			<li v-for="(s, i) in actions.suggest.slice(0, 8)" :key="'g' + i">
				<span class="pair">{{ s.aLabel }} <span class="arr">↔</span> {{ s.bLabel }}</span>
				<span class="m">
					<button v-if="canAct" class="wg-add" @click="$emit('add-link', s)">
						+ link
					</button>
					<template v-else>{{ s.score }}</template>
				</span>
			</li>
		</Sec>
		<Sec
			title="Orphans (unlinked — adopt them)"
			:n="actions.orphans.length"
			empty="Every page is linked"
		>
			<li v-for="(p, i) in actions.orphans.slice(0, 8)" :key="'o' + i">
				<a href="#" @click.prevent="$emit('pick', p.id)">{{ p.label || p.slug }}</a>
			</li>
		</Sec>
		<Sec
			title="Bus-factor risk (single author)"
			:n="actions.busFactor.length"
			empty="No single points of failure"
			warn
		>
			<li v-for="(p, i) in actions.busFactor.slice(0, 8)" :key="'b' + i">
				<a href="#" @click.prevent="$emit('pick', p.id)">{{ p.label || p.slug }}</a>
				<span class="m">{{ p.author }}</span>
			</li>
		</Sec>
		<Sec
			title="Near-duplicate titles (merge?)"
			:n="actions.duplicates.length"
			empty="No duplicates"
		>
			<li v-for="(d, i) in actions.duplicates.slice(0, 6)" :key="'d' + i">
				<span>{{ d.title }}</span
				><span class="m">{{ d.slugs.length }} pages</span>
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
	name: "ActionsTab",
	components: { Sec },
	props: {
		actions: {
			type: Object,
			default: () => ({
				stale: [],
				orphans: [],
				busFactor: [],
				duplicates: [],
				suggest: [],
			}),
		},
		canAct: { type: Boolean, default: false },
	},
	emits: ["pick", "add-link"],
};
</script>

<style scoped>
/* Sec's own markup is h()-rendered, not from this SFC's <template>, so it
   never gets the scoped data-v attribute — :deep() targets it by ancestry.
   .m/.arr/.wg-add live in the default slot content (this SFC's own template),
   stay as-is. */
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
.wg-sec .arr {
	color: var(--text-muted, #aaa);
	margin: 0 3px;
}
.wg-add {
	font-size: 10px;
	padding: 1px 7px;
	border: 1px solid var(--bg-blue, #4c9aff);
	color: var(--bg-blue, #4c9aff);
	background: transparent;
	border-radius: 10px;
	cursor: pointer;
}
.wg-add:hover {
	background: var(--bg-blue, #4c9aff);
	color: #fff;
}
:deep(.wg-empty) {
	font-size: 12px;
}
</style>
