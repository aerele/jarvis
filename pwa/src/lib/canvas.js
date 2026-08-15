// How to show one canvas item. The backend's `type` is authoritative when it is
// set, but agent-written artifacts don't always carry one — fall back to the
// extension rather than dropping the item on the floor.

const IMG_RE = /\.(png|jpe?g|gif|webp|avif|bmp|heic)$/i;
const SHEET_RE = /\.(xlsx|xls|csv)$/i;
const TEXT_RE = /\.(txt|md|json|ya?ml|log|csv)$/i;

export function previewKind(item) {
	const t = String(item?.type || "").toLowerCase();
	if (t.includes("image")) return "image";
	if (t.includes("pdf")) return "pdf";
	if (t.includes("svg")) return "svg";
	if (t.includes("html")) return "html";

	const url = String(item?.file_url || item?.name || "");
	if (IMG_RE.test(url)) return "image";
	if (/\.pdf$/i.test(url)) return "pdf";
	if (/\.svg$/i.test(url)) return "svg";
	if (/\.html?$/i.test(url)) return "html";
	if (SHEET_RE.test(url)) return "sheet";
	if (TEXT_RE.test(url)) return "text";
	return "file";
}

/** "INVOICE.PDF" → "PDF". Shown on the artifact chip. */
export function fileExt(item) {
	const m = String(item?.name || item?.file_url || "").match(/\.([a-z0-9]+)$/i);
	return m ? m[1].toUpperCase() : "FILE";
}
