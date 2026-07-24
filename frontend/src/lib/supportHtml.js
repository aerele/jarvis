// The support message-body pipeline: rewrite Helpdesk's inline /files/ URLs to
// the bench's authenticated same-origin proxy, THEN sanitize.
//
// Order is load-bearing and non-negotiable: DOMPurify runs LAST, so nothing can
// reintroduce an attribute after sanitization. (The page this replaces got the
// order right and said so twice; keep it that way.)
import DOMPurify from "dompurify";
import { supportDownloadUrl } from "@/api";

const LOCAL_FILE = /^\/(private\/)?files\//;

export function renderSupportHtml(rawHtml, ticketName) {
	if (!rawHtml) return "";
	const doc = new DOMParser().parseFromString(String(rawHtml), "text/html");

	// Iterate every img, not just img[src]: an <img> carrying only srcset and no
	// src is valid HTML, and srcset is in DOMPurify's default ALLOWED_ATTR, so a
	// src-only query would let it survive sanitization untouched.
	for (const img of doc.querySelectorAll("img")) {
		const src = img.getAttribute("src") || "";
		if (LOCAL_FILE.test(src)) img.setAttribute("src", supportDownloadUrl(ticketName, src));
		// srcset would bypass the proxy entirely — the browser may pick a
		// candidate from it over src, so it has to go, not be rewritten.
		img.removeAttribute("srcset");
	}
	for (const a of doc.querySelectorAll("a[href]")) {
		const href = a.getAttribute("href") || "";
		if (LOCAL_FILE.test(href)) a.setAttribute("href", supportDownloadUrl(ticketName, href));
	}

	return DOMPurify.sanitize(doc.body.innerHTML);
}
