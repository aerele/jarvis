<template>
	<div class="wg-health" v-if="health">
		<div class="wg-score-row">
			<div class="wg-score" :style="{ color: tone }">{{ health.score }}</div>
			<div class="wg-score-lbl">
				<div class="wg-h">Knowledge Health</div>
				<div class="text-muted">{{ health.pages }} pages · {{ grade }}</div>
			</div>
		</div>
		<div class="wg-bars">
			<div v-for="b in bars" :key="b.k" class="wg-bar">
				<span class="wg-bar-l">{{ b.k }}</span>
				<span class="wg-bar-track"
					><i :style="{ width: pct(b.v), background: tone }"></i
				></span>
				<span class="wg-bar-v">{{ pct(b.v) }}</span>
			</div>
		</div>
	</div>
</template>

<script>
export default {
	name: "HealthPanel",
	props: { health: { type: Object, default: null } },
	computed: {
		tone() {
			const s = (this.health && this.health.score) || 0;
			return s >= 75 ? "#2ea043" : s >= 50 ? "#d6a417" : "#d9534f";
		},
		grade() {
			const s = (this.health && this.health.score) || 0;
			return s >= 75 ? "healthy" : s >= 50 ? "needs care" : "at risk";
		},
		bars() {
			const h = this.health || {};
			return [
				{ k: "freshness", v: h.freshness },
				{ k: "connectivity", v: h.connectivity },
				{ k: "coverage", v: h.coverage },
				{ k: "bus-factor", v: h.bus_factor },
			];
		},
	},
	methods: {
		pct(v) {
			return Math.round((v || 0) * 100) + "%";
		},
	},
};
</script>

<style scoped>
.wg-health {
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 8px;
	padding: 12px;
}
.wg-score-row {
	display: flex;
	align-items: center;
	gap: 12px;
	margin-bottom: 10px;
}
.wg-score {
	font-size: 34px;
	font-weight: 700;
	line-height: 1;
}
.wg-h {
	font-weight: 600;
}
.wg-bars {
	display: flex;
	flex-direction: column;
	gap: 5px;
}
.wg-bar {
	display: grid;
	grid-template-columns: 80px 1fr 34px;
	align-items: center;
	gap: 8px;
	font-size: 11px;
}
.wg-bar-l {
	color: var(--text-muted, #888);
}
.wg-bar-track {
	height: 6px;
	background: var(--control-bg, #eef0f2);
	border-radius: 3px;
	overflow: hidden;
}
.wg-bar-track i {
	display: block;
	height: 100%;
	border-radius: 3px;
}
.wg-bar-v {
	text-align: right;
	color: var(--text-muted, #888);
}
</style>
