// Shared by FilePreview (user attachments) and ChatView's artifact panel
// (assistant-generated files): how a backend preview_file table response is
// stored, and which of its charts belong to the sheet on screen.

// preview_file response -> the table fields kept on the view/artifact
export function tablePreviewFields(r) {
	return { sheets: r.sheets, charts: Array.isArray(r.charts) ? r.charts : [] };
}

// charts anchored on the given sheet (jarvis-chart specs for JvChart)
export function chartsForSheet(charts, sheetName) {
	return (charts || []).filter((c) => c.sheet === sheetName);
}
