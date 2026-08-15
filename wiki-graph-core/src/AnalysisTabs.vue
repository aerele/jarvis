<template>
	<div class="wg-tabs">
		<!-- Priority review cards -->
		<div class="wg-prio" v-if="showPriority">
			<div class="wg-card">
				<span class="wg-card-n">{{ hubName }}</span
				><span class="wg-card-l">top hub</span>
			</div>
			<div class="wg-card">
				<span class="wg-card-n">{{ brokerName }}</span
				><span class="wg-card-l">key bridge</span>
			</div>
			<div class="wg-card" :class="{ warn: staleN }">
				<span class="wg-card-n">{{ staleN }}</span
				><span class="wg-card-l">to fix</span>
			</div>
			<div class="wg-card" :class="{ warn: orphanN }">
				<span class="wg-card-n">{{ orphanN }}</span
				><span class="wg-card-l">orphans</span>
			</div>
		</div>

		<div class="wg-tabbar">
			<button
				v-for="t in TABS"
				:key="t.v"
				:class="['wg-tab', tab === t.v ? 'active' : '']"
				@click="tab = t.v"
			>
				{{ t.label }}
			</button>
		</div>

		<div class="wg-tabbody">
			<DemandPanel
				v-if="tab === 'structure'"
				:lists="analysis.lists"
				:metrics="analysis.metrics"
				@pick="onNodeId"
			/>
			<SuggestPanel
				v-else-if="tab === 'similar'"
				:lists="analysis.lists"
				:communities="analysis.communities"
				:can-add-link="canAct"
				@pick="$emit('pick', $event)"
				@add-link="$emit('add-link', $event)"
			/>
			<EvolutionTab v-else-if="tab === 'evolution'" :nodes="nodes" :history="history" />
			<ActionsTab
				v-else-if="tab === 'actions'"
				:actions="actions"
				:can-act="canAct"
				@pick="$emit('pick', $event)"
				@add-link="$emit('add-link', $event)"
			/>
		</div>
	</div>
</template>

<script>
// The four-tab analysis shell (Obsidian parity): Structure / Similar / Evolution
// / Actions, plus a priority-review card strip. Presentational — data in, events
// out; each surface wires fetching + the (tenant-only) add-link action.
import DemandPanel from "./DemandPanel.vue";
import SuggestPanel from "./SuggestPanel.vue";
import EvolutionTab from "./EvolutionTab.vue";
import ActionsTab from "./ActionsTab.vue";

export default {
	name: "AnalysisTabs",
	components: { DemandPanel, SuggestPanel, EvolutionTab, ActionsTab },
	props: {
		analysis: { type: Object, default: () => ({ metrics: {}, lists: {}, communities: {} }) },
		nodes: { type: Array, default: () => [] },
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
		history: { type: Array, default: () => [] },
		canAct: { type: Boolean, default: false },
		// Priority-card strip (top hub / bridge / to-fix / orphans). On for the
		// operator graph; the tenant graph turns it off for a cleaner surface.
		showPriority: { type: Boolean, default: true },
		// Actions tab (operator curation surface). Off for the tenant graph.
		showActionsTab: { type: Boolean, default: true },
	},
	emits: ["pick", "add-link"],
	data() {
		return { tab: "structure" };
	},
	computed: {
		TABS() {
			const t = [
				{ v: "structure", label: "Structure" },
				{ v: "similar", label: "Similar" },
				{ v: "evolution", label: "Evolution" },
			];
			if (this.showActionsTab) t.push({ v: "actions", label: "Actions" });
			return t;
		},
		lists() {
			return this.analysis.lists || {};
		},
		hubName() {
			return (
				(this.lists.hubs &&
					this.lists.hubs[0] &&
					(this.lists.hubs[0].label || this.lists.hubs[0].slug)) ||
				"—"
			);
		},
		brokerName() {
			return (
				(this.lists.brokers &&
					this.lists.brokers[0] &&
					(this.lists.brokers[0].label || this.lists.brokers[0].slug)) ||
				"—"
			);
		},
		staleN() {
			return (this.actions.stale || []).length;
		},
		orphanN() {
			return (this.actions.orphans || []).length;
		},
	},
	methods: {
		onNodeId(node) {
			this.$emit("pick", node && node.id ? node.id : node);
		},
	},
};
</script>

<style scoped>
.wg-tabs {
	display: flex;
	flex-direction: column;
	gap: 10px;
}
.wg-prio {
	display: grid;
	grid-template-columns: repeat(4, 1fr);
	gap: 6px;
}
.wg-card {
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 8px;
	padding: 6px 8px;
	text-align: center;
	overflow: hidden;
}
.wg-card.warn {
	border-color: #f0c0c0;
}
.wg-card-n {
	display: block;
	font-weight: 600;
	font-size: 13px;
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.wg-card.warn .wg-card-n {
	color: #d9534f;
}
.wg-card-l {
	font-size: 10px;
	color: var(--text-muted, #999);
	text-transform: uppercase;
}
.wg-tabbar {
	display: flex;
	gap: 4px;
	border-bottom: 1px solid var(--border-color, #e2e6ea);
}
.wg-tab {
	font-size: 12px;
	padding: 6px 10px;
	border: none;
	background: transparent;
	cursor: pointer;
	color: var(--text-muted, #888);
	border-bottom: 2px solid transparent;
	margin-bottom: -1px;
}
.wg-tab.active {
	color: var(--text-color, #222);
	border-bottom-color: var(--bg-blue, #4c9aff);
	font-weight: 500;
}
.wg-tabbody {
	border: 1px solid var(--border-color, #e2e6ea);
	border-top: none;
	border-radius: 0 0 8px 8px;
	padding: 12px;
}
</style>
