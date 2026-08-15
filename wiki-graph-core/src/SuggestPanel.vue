<template>
	<div class="wg-sug">
		<Sec
			title="Suggested links (should connect)"
			:n="lists.suggestedLinks.length"
			empty="No suggestions"
		>
			<li v-for="(s, i) in lists.suggestedLinks.slice(0, 8)" :key="'s' + i">
				<span class="pair"
					><a href="#" @click.prevent="$emit('pick', s.a)">{{ s.aLabel }}</a>
					<span class="arr">↔</span>
					<a href="#" @click.prevent="$emit('pick', s.b)">{{ s.bLabel }}</a></span
				>
				<span class="m">
					<button v-if="canAddLink" class="wg-add" @click="$emit('add-link', s)">
						+ link
					</button>
					<template v-else>{{ s.score }}</template>
				</span>
			</li>
		</Sec>
		<Sec
			title="Read together (implicit)"
			:n="lists.coRead.length"
			empty="No co-read pairs yet"
		>
			<li v-for="(c, i) in lists.coRead.slice(0, 8)" :key="'c' + i">
				<span class="pair">{{ c.aLabel }} <span class="arr">↔</span> {{ c.bLabel }}</span>
				<span class="m">{{ c.count }}×</span>
			</li>
		</Sec>
		<Sec
			title="Broker pages (bridge clusters)"
			:n="lists.brokers.length"
			empty="No brokers"
			warn
		>
			<li v-for="(p, i) in lists.brokers.slice(0, 6)" :key="'b' + i">
				<a href="#" @click.prevent="$emit('pick', p.id)">{{ p.label || p.slug }}</a>
				<span class="m">bridge</span>
			</li>
		</Sec>
		<Sec title="Topic clusters" :n="clusterList.length" empty="No clusters">
			<li v-for="(c, i) in clusterList.slice(0, 8)" :key="'t' + i">
				<span>{{ c.label }}</span
				><span class="m">{{ c.size }} pages</span>
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
	name: "SuggestPanel",
	components: { Sec },
	props: {
		lists: { type: Object, default: () => ({ suggestedLinks: [], coRead: [], brokers: [] }) },
		communities: { type: Object, default: () => ({}) },
		canAddLink: { type: Boolean, default: false },
	},
	emits: ["pick", "add-link"],
	computed: {
		clusterList() {
			return Object.values(this.communities || {})
				.filter((c) => c.size > 1)
				.sort((a, b) => b.size - a.size);
		},
	},
};
</script>

<style scoped>
/* Sec's own markup (wg-sec/wg-sec-h/wg-n/ul/wg-empty) is h()-rendered, not
   from this SFC's <template>, so it never gets the scoped data-v attribute —
   :deep() targets it by ancestry. .pair/.arr/.m/.wg-add live in the default
   slot content, which IS this SFC's own template, so those stay scoped as-is. */
.wg-sug {
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
.wg-sec .pair {
	overflow: hidden;
	text-overflow: ellipsis;
}
.wg-sec .arr {
	color: var(--text-muted, #aaa);
	margin: 0 3px;
}
.wg-sec .m {
	color: var(--text-muted, #999);
	white-space: nowrap;
	font-size: 11px;
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
