// The echarts runtime, as raw source, for injection into the sandboxed dashboard
// srcdoc. Loaded only when the dashboard html actually references echarts; the
// deep filesystem-relative ?raw import ships it as its own lazy chunk (echarts'
// exports map blocks bare deep imports).
//
// This lives in its own module (rather than inline in DashboardCanvas.rebuild())
// so the async boundary is isolated and mockable: rebuild() awaits this, and
// overlapping rebuilds race on whichever load resolves last — the canvas guards
// that race with a generation counter, and the test drives it through here.
export async function loadEchartsSource(html) {
	if (!/\becharts\b/i.test(String(html || ""))) return "";
	const mod = await import("../../node_modules/echarts/dist/echarts.min.js?raw");
	return (mod && mod.default) || "";
}
