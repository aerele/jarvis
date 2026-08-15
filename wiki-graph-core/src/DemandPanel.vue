<template>
	<div class="wg-demand">
		<Section title="Hubs (most connected)" :items="lists.hubs" empty="No links yet">
			<template #row="{ it }">
				<a href="#" @click.prevent="$emit('pick', it)">{{ it.label || it.slug }}</a>
				<span class="wg-meta">{{ (metrics[it.id] || {}).degree || 0 }} links</span>
			</template>
		</Section>
		<Section title="Most in demand" :items="lists.mostRead" empty="No reads recorded">
			<template #row="{ it }">
				<a href="#" @click.prevent="$emit('pick', it)">{{ it.label || it.slug }}</a>
				<span class="wg-meta">{{ it.demand }} reads</span>
			</template>
		</Section>
		<Section
			title="Knowledge debt (hot + stale)"
			:items="lists.debt"
			empty="None — knowledge is fresh"
			warn
		>
			<template #row="{ it }">
				<a href="#" @click.prevent="$emit('pick', it)">{{ it.label || it.slug }}</a>
				<span class="wg-meta"
					>{{ it.demand }} reads ·
					{{ it.contradiction ? "contradiction" : "stale" }}</span
				>
			</template>
		</Section>
		<Section
			title="Latent demand (searched, missing)"
			:items="lists.gaps"
			empty="No gaps detected"
			warn
		>
			<template #row="{ it }">
				<span>{{ it.query_norm }}</span>
				<span class="wg-meta"
					>asked {{ it.asked }}× · {{ it.hit ? "thin" : "missing" }}</span
				>
			</template>
		</Section>
		<Section title="Orphans (no links)" :items="lists.orphans" empty="Every page is linked">
			<template #row="{ it }">
				<a href="#" @click.prevent="$emit('pick', it)">{{ it.label || it.slug }}</a>
			</template>
		</Section>
	</div>
</template>

<script>
import { h } from "vue";
// runtime-only Vue can't compile template strings — render fn instead (#1)
const Section = {
	name: "Section",
	props: { title: String, items: Array, empty: String, warn: Boolean },
	render() {
		const items = this.items || [];
		return h("div", { class: "wg-sec" }, [
			h("div", { class: ["wg-sec-h", { warn: this.warn }] }, [
				this.title + " ",
				h("span", { class: "wg-n" }, String(items.length)),
			]),
			items.length
				? h(
						"ul",
						null,
						items
							.slice(0, 8)
							.map((it, i) =>
								h(
									"li",
									{ key: i },
									this.$slots.row ? this.$slots.row({ it }) : undefined
								)
							)
				  )
				: h("div", { class: "wg-empty text-muted" }, h("i", null, this.empty)),
		]);
	},
};
export default {
	name: "DemandPanel",
	components: { Section },
	props: {
		lists: {
			type: Object,
			default: () => ({ hubs: [], mostRead: [], debt: [], gaps: [], orphans: [] }),
		},
		metrics: { type: Object, default: () => ({}) },
	},
	emits: ["pick"],
};
</script>

<style scoped>
/* Section's own markup is rendered via h(), not this SFC's <template>, so it
   never gets the scoped data-v attribute — :deep() targets it by ancestry instead */
:deep(.wg-sec) {
	margin-bottom: 14px;
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
.wg-meta {
	color: var(--text-muted, #999);
	white-space: nowrap;
	font-size: 11px;
}
:deep(.wg-empty) {
	font-size: 12px;
}
</style>
