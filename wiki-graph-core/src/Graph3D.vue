<template>
	<div class="wg3d">
		<div v-if="noWebgl" class="wg3d-fallback">
			<div class="wg3d-fallback-inner">
				<div class="wg3d-fallback-icon">◍</div>
				<p><strong>3D graph unavailable</strong></p>
				<p class="text-muted">
					WebGL is disabled or unsupported in this browser. The analysis cards still work
					— use them to explore hubs, gaps, and suggested links.
				</p>
			</div>
		</div>
		<template v-else>
			<div
				class="wg3d-canvas"
				ref="el"
				role="img"
				aria-label="3D knowledge graph. This canvas is decorative; use the analysis cards and lists beside it to explore the same data by keyboard."
			></div>
			<span
				v-if="shownCount < totalCount"
				class="wg3d-cap"
				:title="`Rendering the ${shownCount} most-connected of ${totalCount} nodes for performance`"
			>
				showing {{ shownCount }} of {{ totalCount }}
			</span>
		</template>
	</div>
</template>

<script>
// The 3D renderer (3d-force-graph / three.js) — drop-in replacement for the
// sigma GraphCanvas: same props/emits contract. Continuously-simulated force
// layout (the "lively" feel), centrality/demand sizing + kind/community color
// from graphStyle, hover-neighbor highlight. Lazy-imports three so it stays off
// first paint. Detects WebGL and degrades to a cards-only message (R5). Caps the
// rendered node count and pauses the render loop when hidden/off-screen (R4).
import { onMounted, onBeforeUnmount, watch, ref } from "vue";
import { nodeStyle, edgeStyle } from "./graphStyle.js";

export function webglAvailable() {
	try {
		const c = document.createElement("canvas");
		return !!(
			window.WebGLRenderingContext &&
			(c.getContext("webgl") || c.getContext("experimental-webgl"))
		);
	} catch (_) {
		return false;
	}
}

export default {
	name: "Graph3D",
	props: {
		data: { type: Object, required: true },
		metrics: { type: Object, default: () => ({}) },
		mode: { type: String, default: "kind" },
		dark: { type: Boolean, default: false },
		// Hard cap on rendered nodes (R4). Above this we keep the most-connected /
		// biggest nodes so the graph stays legible and the sim stays responsive.
		maxNodes: { type: Number, default: 1800 },
	},
	emits: ["node-click"],
	setup(props, { emit }) {
		const el = ref(null);
		const noWebgl = ref(!webglAvailable());
		const shownCount = ref(0),
			totalCount = ref(0);
		let ForceGraph3D = null,
			graph = null,
			ro = null,
			io = null;
		let hoverNode = null,
			adj = {};
		const hlNodes = new Set(),
			hlLinks = new Set();

		async function libs() {
			if (!ForceGraph3D) ForceGraph3D = (await import("3d-force-graph")).default;
		}

		function build() {
			const d = props.data || { nodes: [], edges: [] };
			const ids = new Set(),
				all = [];
			for (const n of d.nodes || []) {
				if (!n || !n.id || ids.has(n.id)) continue;
				ids.add(n.id);
				const s = nodeStyle(n, props.metrics[n.id] || {}, {
					mode: props.mode,
					dark: props.dark,
				});
				all.push({
					id: n.id,
					_node: n,
					__size: s.size || 6,
					__color: s.color,
					__label: s.label,
				});
			}
			totalCount.value = all.length;
			// Node-cap (R4): keep the biggest/most-connected nodes when over budget.
			let nodes = all;
			if (all.length > props.maxNodes) {
				nodes = all
					.slice()
					.sort((a, b) => b.__size - a.__size)
					.slice(0, props.maxNodes);
			}
			shownCount.value = nodes.length;
			const keep = new Set(nodes.map((n) => n.id));
			const seen = new Set(),
				links = [];
			adj = {};
			for (const e of d.edges || []) {
				if (!e || !keep.has(e.source) || !keep.has(e.target) || e.source === e.target)
					continue;
				const key =
					e.source < e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`;
				if (seen.has(key)) continue;
				seen.add(key);
				const st = edgeStyle(e, { dark: props.dark });
				links.push({
					source: e.source,
					target: e.target,
					__color: st.color,
					__width: st.size || 1,
				});
				(adj[e.source] = adj[e.source] || new Set()).add(e.target);
				(adj[e.target] = adj[e.target] || new Set()).add(e.source);
			}
			return { nodes, links };
		}

		const dimNode = () => (props.dark ? "#2a2e37" : "#e8eaed");
		const dimLink = () => (props.dark ? "#20242b" : "#f0f1f3");
		const nodeColor = (n) => (!hoverNode || hlNodes.has(n.id) ? n.__color : dimNode());
		const linkColor = (l) => (!hoverNode || hlLinks.has(l) ? l.__color : dimLink());
		const linkWidth = (l) =>
			hoverNode && hlLinks.has(l) ? (l.__width || 1) * 2 : l.__width || 1;

		function highlight(node) {
			hoverNode = node || null;
			hlNodes.clear();
			hlLinks.clear();
			if (node && graph) {
				hlNodes.add(node.id);
				for (const nb of adj[node.id] || []) hlNodes.add(nb);
				for (const l of graph.graphData().links) {
					const s = typeof l.source === "object" ? l.source.id : l.source;
					const t = typeof l.target === "object" ? l.target.id : l.target;
					if (s === node.id || t === node.id) hlLinks.add(l);
				}
			}
			if (graph) graph.nodeColor(nodeColor).linkColor(linkColor).linkWidth(linkWidth);
		}

		// Pause-on-blur / off-screen (R4): stop the rAF render loop when the tab is
		// hidden or the canvas is scrolled out of view; resume when visible again.
		let onScreen = true;
		function updatePause() {
			if (!graph) return;
			const active = onScreen && !document.hidden;
			active ? graph.resumeAnimation() : graph.pauseAnimation();
		}
		const onVisibility = () => updatePause();

		async function render() {
			if (noWebgl.value) return;
			await libs();
			if (!el.value) return;
			if (!graph) {
				graph = ForceGraph3D()(el.value)
					.backgroundColor("rgba(0,0,0,0)")
					.nodeRelSize(1.4)
					.nodeVal((n) => Math.max(1, n.__size))
					.nodeColor(nodeColor)
					.nodeLabel((n) => n.__label || n.id)
					.nodeOpacity(0.95)
					.linkColor(linkColor)
					.linkWidth(linkWidth)
					.linkOpacity(0.5)
					.onNodeClick((n) => emit("node-click", n._node))
					.onNodeHover((n) => highlight(n))
					.cooldownTicks(140);
				resize();
				ro = new ResizeObserver(resize);
				ro.observe(el.value);
				io = new IntersectionObserver((ents) => {
					onScreen = ents.some((e) => e.isIntersecting);
					updatePause();
				});
				io.observe(el.value);
				document.addEventListener("visibilitychange", onVisibility);
			}
			graph.graphData(build());
			updatePause();
		}

		function resize() {
			if (graph && el.value) graph.width(el.value.clientWidth).height(el.value.clientHeight);
		}

		onMounted(render);
		watch(
			() => [props.data, props.mode, props.dark],
			() => {
				hoverNode = null;
				render();
			}
		);
		onBeforeUnmount(() => {
			if (ro) ro.disconnect();
			if (io) io.disconnect();
			document.removeEventListener("visibilitychange", onVisibility);
			if (graph && graph._destructor) graph._destructor();
		});
		return { el, noWebgl, shownCount, totalCount };
	},
};
</script>

<style scoped>
.wg3d {
	position: relative;
	width: 100%;
	height: 72vh;
	min-height: 420px;
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 8px;
	overflow: hidden;
	background: var(--card-bg, transparent);
}
.wg3d-canvas {
	width: 100%;
	height: 100%;
}
.wg3d-cap {
	position: absolute;
	bottom: 8px;
	right: 10px;
	font-size: 11px;
	color: var(--text-muted, #888);
	background: var(--card-bg, rgba(255, 255, 255, 0.7));
	border: 1px solid var(--border-color, #e2e6ea);
	border-radius: 10px;
	padding: 1px 9px;
	pointer-events: none;
}
.wg3d-fallback {
	display: flex;
	align-items: center;
	justify-content: center;
	height: 100%;
	padding: 24px;
}
.wg3d-fallback-inner {
	text-align: center;
	max-width: 360px;
}
.wg3d-fallback-icon {
	font-size: 40px;
	opacity: 0.4;
	margin-bottom: 8px;
}
.wg3d-fallback p {
	margin: 4px 0;
	font-size: 13px;
}
</style>
