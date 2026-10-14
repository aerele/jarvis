// Copy a rendered reply the way it looks on screen: rich editors (Docs, Gmail,
// Word, Sheets) take the text/html flavour with headings, lists and tables
// intact, plain ones (a terminal, WhatsApp) take text/plain, which keeps list
// numbers, bullets and table cells but no Markdown markers like ** or |---|.
// A third, Jarvis-only flavour carries the reply's Markdown, so pasting it back
// into the chat box gives the same message, not the flattened plain text.

/** Clipboard type for the Markdown of a copied reply. Only the chat box reads it. */
export const MARKDOWN_TYPE = "text/x-jarvis-markdown";

const BLOCK = new Set([
	"P",
	"H1",
	"H2",
	"H3",
	"H4",
	"H5",
	"H6",
	"BLOCKQUOTE",
	"PRE",
	"TABLE",
	"UL",
	"OL",
	"HR",
	"DIV",
]);
// Pasted HTML carries none of the app's CSS, so tables and code get just enough
// inline style to stay readable in Gmail, Docs and Word, in light or dark themes.
const CELL_STYLE = "border:1px solid #c8c8c8;padding:4px 8px";
const MONO_STYLE = "font-family:ui-monospace,Menlo,Consolas,monospace";

const collapse = (s) => s.replace(/\s+/g, " ");

function inlineText(el) {
	if (el.tagName === "BR") return "\n";
	let s = "";
	for (const n of el.childNodes) {
		if (n.nodeType === 3) s += collapse(n.nodeValue);
		else if (n.nodeType === 1) s += inlineText(n);
	}
	// A Markdown link whose label is not its address keeps the address in plain
	// text; a document-name link (jv-doclink) stays just the name.
	if (el.tagName === "A" && !el.classList.contains("jv-doclink")) {
		const href = el.getAttribute("href") || "";
		if (href && !sameAddress(s.trim(), href)) s += ` (${href})`;
	}
	return s;
}

// True when a link's label already is its address (as typed, or the app path
// of the absolute href), so plain text need not repeat it.
function sameAddress(label, href) {
	const forms = [href];
	try {
		const u = new URL(href);
		forms.push(u.pathname + u.search + u.hash);
	} catch (e) {
		/* a relative href: compare as is */
	}
	const norm = (x) => {
		try {
			x = decodeURI(x);
		} catch (e) {
			/* keep it encoded */
		}
		return x.replace(/\/$/, "");
	};
	return forms.some((f) => norm(f) === norm(label));
}

function cleanInline(s) {
	return s
		.replace(/[ \t]+/g, " ")
		.replace(/ ?\n ?/g, "\n")
		.trim();
}

function listText(list, depth) {
	const lines = [];
	let n = Number(list.getAttribute("start")) || 1;
	for (const li of list.children) {
		if (li.tagName !== "LI") continue;
		const marker = list.tagName === "OL" ? `${n++}.` : "-";
		let own = "";
		const nested = [];
		for (const c of li.childNodes) {
			if (c.nodeType === 1 && (c.tagName === "UL" || c.tagName === "OL"))
				nested.push(listText(c, depth + 1));
			else if (c.nodeType === 1 && BLOCK.has(c.tagName))
				own += " " + blockText(c, depth + 1);
			else if (c.nodeType === 1) own += inlineText(c);
			else if (c.nodeType === 3) own += collapse(c.nodeValue);
		}
		// A line break inside the item keeps the item's indent.
		const pad = "  ".repeat(depth);
		const item = cleanInline(own).replace(/\n/g, `\n${pad}${" ".repeat(marker.length + 1)}`);
		lines.push(`${pad}${marker} ${item}`, ...nested);
	}
	return lines.join("\n");
}

function blockText(el, depth = 0) {
	if (el.tagName === "PRE" || el.classList.contains("jv-mermaid"))
		return el.textContent.replace(/\n+$/, "");
	if (el.tagName === "HR") return "";
	if (el.tagName === "UL" || el.tagName === "OL") return listText(el, depth);
	if (el.tagName === "TABLE") {
		return (
			[...el.querySelectorAll("tr")]
				// A cell stays on its row: its own line breaks become spaces.
				.map((tr) =>
					[...tr.children]
						.map((c) => cleanInline(inlineText(c)).replace(/\n/g, " "))
						.join("\t")
				)
				.join("\n")
		);
	}
	return blocksOf(el, depth).join("\n\n");
}

function blocksOf(parent, depth = 0) {
	const out = [];
	let run = "";
	const flush = () => {
		const t = cleanInline(run);
		if (t) out.push(t);
		run = "";
	};
	for (const n of parent.childNodes) {
		if (n.nodeType === 3) run += collapse(n.nodeValue);
		else if (n.nodeType !== 1) continue;
		else if (!BLOCK.has(n.tagName)) run += inlineText(n);
		else {
			flush();
			const b = blockText(n, depth);
			if (b) out.push(b);
		}
	}
	flush();
	return out;
}

/** The plain-text form of rendered reply HTML (a node tree, no layout needed). */
export function plainText(root) {
	return blocksOf(root).join("\n\n").trim();
}

/** {html, text} for the clipboard, from the renderer's HTML for a reply. */
export function prepareRich(html, doc = globalThis.document) {
	const box = doc.createElement("div");
	box.innerHTML = html || "";
	// Relative links and images become absolute so they still work wherever the
	// reply is pasted.
	for (const a of box.querySelectorAll("a[href]")) {
		a.setAttribute("href", a.href);
		a.removeAttribute("target");
		a.removeAttribute("rel");
		a.removeAttribute("title");
	}
	for (const img of box.querySelectorAll("img[src]")) img.setAttribute("src", img.src);
	const text = plainText(box);
	// A diagram copies as its source, line breaks kept.
	for (const m of box.querySelectorAll(".jv-mermaid")) {
		const pre = doc.createElement("pre");
		pre.textContent = m.textContent;
		m.replaceWith(pre);
	}
	for (const t of box.querySelectorAll("table"))
		t.setAttribute("style", "border-collapse:collapse");
	for (const c of box.querySelectorAll("th, td")) {
		const align = c.style.textAlign;
		c.setAttribute("style", CELL_STYLE + (align ? `;text-align:${align}` : ""));
	}
	for (const c of box.querySelectorAll("pre, code")) c.setAttribute("style", MONO_STYLE);
	for (const el of box.querySelectorAll("[class]")) el.removeAttribute("class");
	return { html: box.innerHTML, text };
}

// Resolves {ok, reason}; never rejects. The first attempt is synchronous, a
// `copy` event filled in by hand: it runs inside the click's user activation in
// every browser and leaves the user's selection and focus alone. Only when the
// browser refuses that does it try the async Clipboard API.
// `markdown` rides along on the synchronous path only: the async Clipboard API
// accepts no custom types, and that path is the rare fallback.
export function copyRich(
	html,
	{ win = globalThis.window, doc = globalThis.document, markdown = "" } = {}
) {
	let payload;
	try {
		payload = prepareRich(html, doc);
	} catch (e) {
		return Promise.resolve({ ok: false, reason: `prepare_failed:${e?.name || "Error"}` });
	}
	if (!payload.text) return Promise.resolve({ ok: false, reason: "empty" });
	if (copyViaEvent({ ...payload, markdown }, doc)) return Promise.resolve({ ok: true });
	const clip = win?.navigator?.clipboard;
	if (!clip?.write || typeof win.ClipboardItem !== "function") {
		return Promise.resolve({ ok: false, reason: "no_clipboard_api" });
	}
	return new Promise((resolve) => {
		// A synchronous throw from the constructor or write() lands in .then's
		// rejection handler like an async refusal would.
		resolve(
			clip.write([
				new win.ClipboardItem({
					"text/html": new Blob([payload.html], { type: "text/html" }),
					"text/plain": new Blob([payload.text], { type: "text/plain" }),
				}),
			])
		);
	}).then(
		() => ({ ok: true }),
		(e) => ({ ok: false, reason: `write_rejected:${e?.name || "Error"}` })
	);
}

function copyViaEvent({ html, text, markdown }, doc) {
	let filled = false;
	const onCopy = (e) => {
		if (!e.clipboardData) return;
		e.clipboardData.setData("text/html", html);
		e.clipboardData.setData("text/plain", text);
		if (markdown) e.clipboardData.setData(MARKDOWN_TYPE, markdown);
		e.preventDefault();
		filled = true;
	};
	// Capture phase, so this runs before any page-level copy handler.
	doc.addEventListener("copy", onCopy, true);
	try {
		return doc.execCommand("copy") && filled;
	} catch (e) {
		return false;
	} finally {
		doc.removeEventListener("copy", onCopy, true);
	}
}

// The chat's Copy on a reply: render the Markdown the way the bubble shows it,
// copy that, then confirm or report. Kept out of ChatView so the wiring is tested.
// Only the latest click may move the tick: a slow async write for an older
// reply must not replace the newer one's.
let _latest = 0;
export function copyReply(
	id,
	content,
	{ render, markdown = (t) => t, onCopied, onFailed, report, copy = copyRich }
) {
	const token = ++_latest;
	let html, md;
	try {
		html = render(content);
		md = markdown(content);
	} catch (e) {
		report?.(`render_failed:${e?.name || "Error"}`);
		onFailed?.(id, "render_failed");
		return Promise.resolve(false);
	}
	return copy(html, { markdown: md }).then(({ ok, reason }) => {
		if (token !== _latest) return ok;
		if (ok) onCopied(id);
		else {
			if (reason !== "empty") report?.(reason);
			onFailed?.(id, reason);
		}
		return ok;
	});
}

/** The Markdown of a copied Jarvis reply in a paste, or "" when there is none. */
export function pastedMarkdown(clipboardData) {
	try {
		return clipboardData?.getData?.(MARKDOWN_TYPE) || "";
	} catch (e) {
		return "";
	}
}

// Put text at the textarea's caret, replacing any selection. insertText keeps
// the browser's undo history and fires `input` itself, so v-model follows; the
// fallback edits the value directly and fires `input` by hand.
export function insertAtCursor(el, text, doc = globalThis.document) {
	el.focus();
	let done = false;
	try {
		done = doc.execCommand("insertText", false, text);
	} catch (e) {
		done = false;
	}
	if (done) return;
	el.setRangeText(text, el.selectionStart, el.selectionEnd, "end");
	el.dispatchEvent(new Event("input", { bubbles: true }));
}
