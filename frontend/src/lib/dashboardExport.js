// dashboardExport - parent-side halves of the dashboard export flow.
// DashboardCanvas owns the postMessage plumbing (it posts {type:"export", id,
// format, lib, pixelRatio} into the sandboxed iframe and resolves the matching
// export:result frame); this module owns what sits around that exchange:
//   - loadCaptureLib(): the html-to-image UMD source as a string (dynamic
//     ?raw import so the capture lib stays out of the main chunk; injected
//     into the iframe via script.textContent because the iframe's CSP blocks
//     every external fetch),
//   - downloadPng(images, title): first captured image → Blob → <a download>,
//   - downloadPdf(images, title): captured slides → lazy jspdf → one page per
//     slide, page size = the slide's pixel size.

let _libSource = null;
export async function loadCaptureLib() {
	if (_libSource == null) {
		// Bare deep import is fine here - html-to-image has no exports map;
		// dist/html-to-image.js is the UMD build that installs window.htmlToImage.
		const mod = await import("html-to-image/dist/html-to-image.js?raw");
		_libSource = (mod && mod.default) || "";
	}
	return _libSource;
}

function slugify(title) {
	return (
		String(title || "")
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, "-")
			.replace(/^-+|-+$/g, "") || "dashboard"
	);
}

function dataUrlToBlob(dataUrl) {
	const [meta, b64] = String(dataUrl || "").split(",");
	const mime = (/data:([^;]+)/.exec(meta) || [])[1] || "image/png";
	const bin = atob(b64 || "");
	const bytes = new Uint8Array(bin.length);
	for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
	return new Blob([bytes], { type: mime });
}

// images: [{dataUrl, w, h, title}] from the iframe's export:result frame.
export function downloadPng(images, title) {
	const img = images && images[0];
	if (!img || !img.dataUrl) throw new Error("Nothing was captured");
	const url = URL.createObjectURL(dataUrlToBlob(img.dataUrl));
	const a = document.createElement("a");
	a.href = url;
	a.download = slugify(title) + ".png";
	document.body.appendChild(a);
	a.click();
	a.remove();
	setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export async function downloadPdf(images, title) {
	if (!images || !images.length) throw new Error("Nothing was captured");
	// jspdf only loads when a PDF export actually happens.
	const { jsPDF } = await import("jspdf");
	let doc = null;
	for (const img of images) {
		const w = Math.max(1, Math.round(img.w || 1));
		const h = Math.max(1, Math.round(img.h || 1));
		const orientation = w >= h ? "landscape" : "portrait";
		if (!doc) {
			// px_scaling: treat px as real pixels (jspdf otherwise rescales 96dpi→72).
			doc = new jsPDF({ orientation, unit: "px", format: [w, h], hotfixes: ["px_scaling"] });
		} else {
			doc.addPage([w, h], orientation);
		}
		doc.addImage(img.dataUrl, "PNG", 0, 0, w, h);
	}
	doc.save(slugify(title) + ".pdf");
}
