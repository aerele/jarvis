// Pure node/edge styling for the wiki graph (no sigma/graphology import).
// Color by node kind (Org/Role/User/page) or by Louvain community; size by
// centrality × read-demand; knowledge-debt pages go red. Edge color by kind:
// [[links-to]] solid (knowledge), the rest dashed-weight (utilization overlay).

const KIND = { org: "#a970ff", role: "#f2b134", user: "#4c9aff", page: "#8892b0" };
const COMMUNITY = [
	"#7c9eff",
	"#f2b134",
	"#4bd0a0",
	"#ff7ab6",
	"#a970ff",
	"#ff8a5c",
	"#5cc8ff",
	"#c3d82c",
	"#ff5c8a",
	"#9d7bff",
];
const DEBT = "#ff5c5c";

export function nodeStyle(n, m, { mode, dark }) {
	if (n.kind === "cluster") {
		return {
			label: `${n.label} (${n.weight || 1})`,
			size: Math.min(12 + Math.sqrt(n.weight || 1) * 4, 40),
			color: COMMUNITY[(Number(n.community) || 0) % COMMUNITY.length],
		};
	}
	const deg = (m && m.degree) || 0;
	const demand = n.demand || 0;
	const auth = (m && m.authority) || 0;
	let size = 4 + Math.sqrt(deg) * 2.4 + Math.sqrt(demand) * 1.8 + auth * 12;
	if (m && m.betweenness > 0) size += 3; // broker pages stand out
	if (n.kind === "org") size = Math.max(size, 10);
	size = Math.min(size, 28);
	let color;
	if (mode === "community") color = COMMUNITY[((m && m.community) || 0) % COMMUNITY.length];
	else color = KIND[n.kind] || "#888888";
	if (n.debt) color = DEBT;
	return { label: n.label || n.id, size, color };
}

const EDGE_COLORS_DARK = {
	"links-to": "#5a6478",
	scope: "#3a3f4b",
	authored: "#3f6f57",
	"member-of": "#5a4f7a",
	read: "#3a5a80",
	wrote: "#7a5a3a",
	"cluster-link": "#5a6478",
};
const EDGE_COLORS_LIGHT = {
	"links-to": "#aeb6c2",
	scope: "#e2e6ea",
	authored: "#bfe3cf",
	"member-of": "#d8cff0",
	read: "#cfe0f5",
	wrote: "#f0dcc0",
	"cluster-link": "#aeb6c2",
};

export function edgeStyle(e, { dark }) {
	const map = dark ? EDGE_COLORS_DARK : EDGE_COLORS_LIGHT;
	const strong = e.kind === "links-to" || e.kind === "cluster-link";
	return {
		size:
			e.kind === "cluster-link"
				? Math.min(1 + (e.weight || 1) * 0.5, 5)
				: strong
				? 1.4
				: 0.7,
		color: map[e.kind] || (dark ? "#3a3f4b" : "#e2e6ea"),
		type: "line",
	};
}

export const LEGEND = {
	nodes: [
		{ label: "Org", color: KIND.org },
		{ label: "Role", color: KIND.role },
		{ label: "User", color: KIND.user },
		{ label: "Page", color: KIND.page },
		{ label: "Knowledge debt", color: DEBT },
	],
	edges: [
		{ label: "links ([[wiki]])", kind: "links-to" },
		{ label: "scope", kind: "scope" },
		{ label: "authored", kind: "authored" },
		{ label: "read / wrote", kind: "read" },
	],
};
