// Shared by FilePreview (user attachments) and ChatView's artifact panel
// (assistant-generated files): how a backend preview_file table response is
// stored, and which of its charts belong to the sheet on screen.

// preview_file response -> the table fields kept on the view/artifact
export function tablePreviewFields(r) {
	return { sheets: r.sheets, charts: Array.isArray(r.charts) ? r.charts : [] };
}

// Charts for the sheet on screen, plus (on the first tab only) the charts of
// sheets the preview did not load: preview_file reads just the first sheet's
// cells, so a chart anchored on any other sheet would otherwise never show.
// Those carry their own sheet name (see SheetCharts' caption).
export function chartsForSheet(charts, sheetName, loadedNames = []) {
	const loaded = new Set(loadedNames);
	const first = loadedNames[0];
	return (charts || []).filter(
		(c) => c.sheet === sheetName || (sheetName === first && c.sheet && !loaded.has(c.sheet))
	);
}
