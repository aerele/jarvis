<template>
	<div class="wg-evo">
		<div v-if="!rows.length" class="text-muted wg-empty">
			<i>Not enough dated pages to chart growth yet.</i>
		</div>
		<div v-else>
			<div class="wg-evo-head">
				<span
					><strong>{{ total }}</strong> pages<template
						v-if="measured && latestLinks != null"
					>
						· <strong>{{ latestLinks }}</strong> links</template
					></span
				>
				<span class="wg-evo-mode" :class="measured ? 'meas' : 'est'">{{
					measured ? "measured" : "estimated"
				}}</span>
			</div>
			<svg class="wg-evo-svg" viewBox="0 0 320 90" preserveAspectRatio="none">
				<polyline
					v-if="measured"
					:points="linkPoly"
					fill="none"
					:stroke="linkStroke"
					stroke-width="1.5"
					stroke-dasharray="3 3"
				/>
				<polyline :points="pagePoly" fill="none" :stroke="stroke" stroke-width="2" />
				<circle
					v-for="(p, i) in dots"
					:key="i"
					:cx="p.x"
					:cy="p.y"
					r="2.5"
					:fill="stroke"
				/>
			</svg>
			<div class="wg-evo-axis text-muted">
				<span>{{ rows[0].date }}</span
				><span>{{ rows[rows.length - 1].date }}</span>
			</div>
			<div v-if="measured" class="wg-evo-legend text-muted">
				<span><span class="sw" :style="{ background: stroke }"></span>pages</span>
				<span
					><span class="sw dash" :style="{ borderColor: linkStroke }"></span>links</span
				>
			</div>
			<div v-if="importNote" class="wg-evo-note">⚠ {{ importNote }}</div>
		</div>
	</div>
</template>

<script>
// Knowledge Evolution. Prefers the MEASURED daily series (get_wiki_graph_history:
// real page + link growth, orphan decline recorded once/day) when present; else
// falls back to RECONSTRUCTING cumulative pages from node.created (day precision).
// R9 data-quality (reconstruction only): drop invalid/future dates, annotate a
// bulk-import spike so a one-day jump isn't read as organic growth.
const W = 320,
	H = 90,
	PAD = 6;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export default {
	name: "EvolutionTab",
	props: {
		nodes: { type: Array, default: () => [] },
		// Measured daily totals [{date, pages, links, orphans, ...}]; empty → reconstruct.
		history: { type: Array, default: () => [] },
	},
	computed: {
		measured() {
			return Array.isArray(this.history) && this.history.length > 0;
		},
		perDay() {
			const today = new Date().toISOString().slice(0, 10);
			const counts = {};
			for (const n of this.nodes) {
				if (n.kind !== "page") continue;
				const d = n.created;
				if (!DATE_RE.test(d) || d > today) continue; // drop invalid/future (R9)
				counts[d] = (counts[d] || 0) + 1;
			}
			return counts;
		},
		reconRows() {
			const dates = Object.keys(this.perDay).sort();
			let cum = 0;
			return dates.map((date) => {
				cum += this.perDay[date];
				return { date, pages: cum, links: null };
			});
		},
		rows() {
			if (this.measured) {
				return this.history
					.filter((h) => DATE_RE.test(h.date))
					.map((h) => ({ date: h.date, pages: +h.pages || 0, links: +h.links || 0 }));
			}
			return this.reconRows;
		},
		total() {
			return this.rows.length ? this.rows[this.rows.length - 1].pages : 0;
		},
		latestLinks() {
			return this.rows.length ? this.rows[this.rows.length - 1].links : null;
		},
		maxY() {
			let m = 1;
			for (const r of this.rows) m = Math.max(m, r.pages, r.links || 0);
			return m;
		},
		pagePoly() {
			return this._poly((r) => r.pages);
		},
		linkPoly() {
			return this.measured ? this._poly((r) => r.links || 0) : "";
		},
		dots() {
			return this._scaled((r) => r.pages);
		},
		importNote() {
			if (this.measured) return ""; // real series — no reconstruction caveat
			const total = this.total;
			let spikeDate = null,
				spikeN = 0;
			for (const [date, n] of Object.entries(this.perDay)) {
				if (n > spikeN) {
					spikeN = n;
					spikeDate = date;
				}
			}
			if (spikeDate && spikeN >= Math.max(3, 0.4 * total)) {
				return `${spikeN} pages appeared on ${spikeDate} — likely a bulk import, not organic growth.`;
			}
			return "";
		},
		stroke() {
			return "#4c9aff";
		},
		linkStroke() {
			return "#9b8cff";
		},
	},
	methods: {
		_scaled(sel) {
			const rows = this.rows,
				n = rows.length,
				maxY = this.maxY;
			if (!n) return [];
			return rows.map((r, i) => ({
				x: PAD + (n === 1 ? (W - 2 * PAD) / 2 : (i / (n - 1)) * (W - 2 * PAD)),
				y: H - PAD - (sel(r) / maxY) * (H - 2 * PAD),
			}));
		},
		_poly(sel) {
			return this._scaled(sel)
				.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`)
				.join(" ");
		},
	},
};
</script>

<style scoped>
.wg-evo {
	padding: 4px 2px;
}
.wg-evo-head {
	display: flex;
	justify-content: space-between;
	align-items: center;
	font-size: 12px;
	margin-bottom: 6px;
}
.wg-evo-mode {
	font-size: 10px;
	text-transform: uppercase;
	letter-spacing: 0.03em;
	border-radius: 8px;
	padding: 1px 7px;
}
.wg-evo-mode.meas {
	background: var(--surface-blue-2, #e6f0ff);
	color: var(--ink-blue-3, #2f6fd0);
}
.wg-evo-mode.est {
	background: var(--control-bg, #f0f1f3);
	color: var(--text-muted, #888);
}
.wg-evo-svg {
	width: 100%;
	height: 90px;
}
.wg-evo-axis {
	display: flex;
	justify-content: space-between;
	font-size: 10px;
	margin-top: 2px;
}
.wg-evo-legend {
	display: flex;
	gap: 14px;
	font-size: 10px;
	margin-top: 6px;
}
.wg-evo-legend .sw {
	display: inline-block;
	width: 12px;
	height: 2px;
	vertical-align: middle;
	margin-right: 4px;
}
.wg-evo-legend .sw.dash {
	height: 0;
	border-top: 2px dashed;
}
.wg-evo-note {
	font-size: 11px;
	color: #d6a417;
	margin-top: 8px;
}
.wg-empty {
	font-size: 12px;
	padding: 16px 4px;
}
</style>
