<template>
	<div class="wg-exp" v-if="experts">
		<div class="wg-h">{{ selectedSlug ? "Experts on this page" : "Top experts" }}</div>
		<div v-if="!rows.length" class="text-muted wg-empty"><i>No contributions yet.</i></div>
		<ol v-else class="wg-exp-list">
			<li v-for="(r, i) in rows" :key="i">
				<span class="wg-rank">{{ i + 1 }}</span>
				<span class="wg-user">{{ r.user }}</span>
				<span class="wg-meta">{{ r.score }}</span>
			</li>
		</ol>
		<div v-if="selectedSlug" class="wg-hint text-muted">
			go-to people for <code>{{ selectedSlug }}</code>
		</div>
	</div>
</template>

<script>
export default {
	name: "ExpertPanel",
	props: {
		experts: { type: Object, default: null },
		selectedSlug: { type: String, default: null },
	},
	computed: {
		rows() {
			if (!this.experts) return [];
			if (this.selectedSlug && this.experts.per_page) {
				return this.experts.per_page[this.selectedSlug] || [];
			}
			return this.experts.top || [];
		},
	},
};
</script>

<style scoped>
.wg-exp {
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 8px;
	padding: 12px;
}
.wg-h {
	font-weight: 600;
	margin-bottom: 6px;
}
.wg-exp-list {
	list-style: none;
	margin: 0;
	padding: 0;
}
.wg-exp-list li {
	display: flex;
	align-items: center;
	gap: 8px;
	font-size: 12px;
	padding: 2px 0;
}
.wg-rank {
	width: 16px;
	color: var(--text-muted, #aaa);
	font-size: 11px;
}
.wg-user {
	flex: 1;
}
.wg-meta {
	color: var(--text-muted, #999);
	font-size: 11px;
}
.wg-hint {
	font-size: 11px;
	margin-top: 6px;
}
.wg-empty {
	font-size: 12px;
}
</style>
