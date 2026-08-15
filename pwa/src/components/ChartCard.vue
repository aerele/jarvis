<script setup>
import { computed } from "vue";

// The agent's ```jarvis-chart``` spec, drawn as plain SVG.
//
// The desktop SPA renders these with ECharts. Pulling ECharts into the phone
// bundle to draw a five-slice pie would roughly triple it, so — like the native
// app — this hand-draws the shapes the agent actually emits (pie/donut/bar/
// line/area) and degrades honestly for the rest.
const props = defineProps({ spec: { type: Object, required: true } });

const PALETTE = [
	"#8269F8",
	"#3B82F6",
	"#E8934E",
	"#22C55E",
	"#EC4899",
	"#14B8A6",
	"#F59E0B",
	"#8B5CF6",
];

const num = (v) => {
	const n = Number(v);
	return Number.isFinite(n) ? n : 0;
};

const type = computed(() => String(props.spec.type || ""));
const isPie = computed(() => type.value === "pie" || type.value === "donut");
const isAxis = computed(() => ["bar", "line", "area"].includes(type.value));

// ── pie / donut ─────────────────────────────────────────────────────────────
const polar = (cx, cy, r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];

function arcPath(cx, cy, rO, rI, a0, a1) {
	const large = a1 - a0 > Math.PI ? 1 : 0;
	const [x1, y1] = polar(cx, cy, rO, a0);
	const [x2, y2] = polar(cx, cy, rO, a1);
	if (rI > 0) {
		const [x3, y3] = polar(cx, cy, rI, a1);
		const [x4, y4] = polar(cx, cy, rI, a0);
		return `M${x1} ${y1} A${rO} ${rO} 0 ${large} 1 ${x2} ${y2} L${x3} ${y3} A${rI} ${rI} 0 ${large} 0 ${x4} ${y4} Z`;
	}
	return `M${cx} ${cy} L${x1} ${y1} A${rO} ${rO} 0 ${large} 1 ${x2} ${y2} Z`;
}

const slices = computed(() => {
	const labels = (props.spec.x || []).map(String);
	const values = (props.spec.series?.[0]?.data || []).map(num);
	const data = values
		.map((v, i) => ({
			name: labels[i] ?? `#${i + 1}`,
			value: v,
			color: PALETTE[i % PALETTE.length],
		}))
		.filter((d) => d.value > 0);
	const total = data.reduce((a, d) => a + d.value, 0) || 1;
	const rI = type.value === "donut" ? 46 : 0;
	let angle = -Math.PI / 2;
	return data.map((d) => {
		const a0 = angle;
		const a1 = angle + (d.value / total) * Math.PI * 2;
		angle = a1;
		return {
			...d,
			pct: Math.round((d.value / total) * 100),
			d: arcPath(100, 100, 78, rI, a0, a1),
		};
	});
});

// ── bar / line / area ───────────────────────────────────────────────────────
const W = 300;
const H = 170;
const PAD = { l: 34, r: 10, t: 8, b: 30 };
const plotW = W - PAD.l - PAD.r;
const plotH = H - PAD.t - PAD.b;

const cats = computed(() => (props.spec.x || []).map(String));
const series = computed(() =>
	(props.spec.series || []).map((s) => ({ name: s.name || "", data: (s.data || []).map(num) }))
);
const slots = computed(() =>
	Math.max(cats.value.length, ...series.value.map((s) => s.data.length), 1)
);
const maxV = computed(() => Math.max(1, ...series.value.flatMap((s) => s.data)));
const step = computed(() => plotW / slots.value);
const y = (v) => PAD.t + plotH - (v / maxV.value) * plotH;

const gridlines = computed(() =>
	[0, 0.5, 1].map((t) => ({ y: PAD.t + plotH * (1 - t), label: Math.round(maxV.value * t) }))
);

const bars = computed(() => {
	if (type.value !== "bar") return [];
	const out = [];
	const n = series.value.length;
	series.value.forEach((s, si) => {
		const bw = (step.value * 0.7) / n;
		s.data.forEach((v, i) => {
			out.push({
				key: `${si}-${i}`,
				x: PAD.l + i * step.value + step.value * 0.15 + si * bw,
				y: y(v),
				w: Math.max(1, bw - 1),
				h: PAD.t + plotH - y(v),
				fill: PALETTE[si % PALETTE.length],
			});
		});
	});
	return out;
});

const lines = computed(() => {
	if (type.value !== "line" && type.value !== "area") return [];
	return series.value.map((s, si) => {
		const pts = s.data.map((v, i) => `${PAD.l + i * step.value + step.value / 2},${y(v)}`);
		const color = PALETTE[si % PALETTE.length];
		const area =
			type.value === "area" && pts.length
				? `M${PAD.l + step.value / 2},${PAD.t + plotH} L${pts.join(" L")} L${
						PAD.l + (s.data.length - 1) * step.value + step.value / 2
				  },${PAD.t + plotH} Z`
				: "";
		return { key: si, d: pts.length ? `M${pts.join(" L")}` : "", area, color };
	});
});

// Six labels max, or they overlap into mush at phone width.
const catEvery = computed(() => Math.ceil(slots.value / 6));
const catLabels = computed(() =>
	cats.value
		.map((c, i) => ({
			i,
			x: PAD.l + i * step.value + step.value / 2,
			text: c.length > 8 ? `${c.slice(0, 7)}…` : c,
		}))
		.filter((c) => c.i % catEvery.value === 0)
);
</script>

<template>
	<div class="jv-chart">
		<div v-if="props.spec.title" class="jv-chart-title">{{ props.spec.title }}</div>

		<div v-if="isPie" class="jv-pie">
			<svg width="180" height="180" viewBox="0 0 200 200">
				<path
					v-for="(s, i) in slices"
					:key="i"
					:d="s.d"
					:fill="s.color"
					stroke="var(--card)"
					stroke-width="1.5"
				/>
			</svg>
			<div class="jv-legend">
				<span v-for="(s, i) in slices" :key="i" class="jv-legend-item">
					<span class="jv-swatch" :style="{ background: s.color }" />{{ s.name }} ·
					{{ s.pct }}%
				</span>
			</div>
		</div>

		<div v-else-if="isAxis">
			<svg width="100%" :height="H" :viewBox="`0 0 ${W} ${H}`">
				<g v-for="(g, i) in gridlines" :key="i">
					<line
						:x1="PAD.l"
						:y1="g.y"
						:x2="W - PAD.r"
						:y2="g.y"
						stroke="var(--border)"
						stroke-width="1"
					/>
					<text
						:x="PAD.l - 5"
						:y="g.y + 3"
						font-size="9"
						fill="var(--ink4)"
						text-anchor="end"
					>
						{{ g.label }}
					</text>
				</g>

				<rect
					v-for="b in bars"
					:key="b.key"
					:x="b.x"
					:y="b.y"
					:width="b.w"
					:height="b.h"
					rx="2"
					:fill="b.fill"
				/>

				<g v-for="l in lines" :key="l.key">
					<path v-if="l.area" :d="l.area" :fill="l.color" fill-opacity="0.15" />
					<path :d="l.d" :stroke="l.color" stroke-width="2" fill="none" />
				</g>

				<text
					v-for="c in catLabels"
					:key="c.i"
					:x="c.x"
					:y="H - 12"
					font-size="9"
					fill="var(--ink5)"
					text-anchor="middle"
				>
					{{ c.text }}
				</text>
			</svg>
			<div v-if="series.length > 1" class="jv-legend">
				<span v-for="(s, i) in series" :key="i" class="jv-legend-item">
					<span
						class="jv-swatch"
						:style="{ background: PALETTE[i % PALETTE.length] }"
					/>{{ s.name || `Series ${i + 1}` }}
				</span>
			</div>
		</div>

		<!-- A heatmap or a gauge is not worth hand-drawing for the phone; say so
		     rather than silently dropping the agent's output. -->
		<div v-else class="jv-chart-na">
			Chart ({{ type }}). Open the full workspace to view it.
		</div>
	</div>
</template>

<style scoped>
.jv-chart {
	margin-top: 8px;
	padding: 13px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
}
.jv-chart-title {
	margin-bottom: 10px;
	font-size: 13px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-pie {
	display: flex;
	flex-direction: column;
	align-items: center;
}
.jv-legend {
	display: flex;
	flex-wrap: wrap;
	justify-content: center;
	gap: 10px;
	margin-top: 4px;
	font-size: 11.5px;
	color: var(--ink7);
}
.jv-legend-item {
	display: inline-flex;
	align-items: center;
	gap: 5px;
}
.jv-swatch {
	width: 9px;
	height: 9px;
	border-radius: 2px;
	flex: none;
}
.jv-chart-na {
	font-size: 12px;
	color: var(--ink5);
}
</style>
