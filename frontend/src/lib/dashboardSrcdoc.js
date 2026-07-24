// dashboardSrcdoc - pure string assembly of the sandboxed dashboard iframe's
// srcdoc (DOM-free so `node --test` covers it). The AI's HTML (full document
// or fragment) gets a head block injected IN ORDER:
//   1. the CSP meta (FIRST head child - it must precede every script),
//   2. <meta charset="utf-8"> (only when we wrap a fragment in our skeleton),
//   3. the echarts source inline (optional - caller dynamic-imports the 1MB
//      min build as ?raw and passes the string, keeping this util sync/pure),
//   4. the bridge runtime inline - BEFORE any user markup, so window.jarvis
//      exists by the time user scripts run.
// The srcdoc iframe is sandbox="allow-scripts" with NO allow-same-origin; the
// CSP blocks all network egress (connect-src 'none', script/style inline-only)
// so a malicious dashboard can neither phone home nor load remote code.
//
// Sources contract (single source of truth): the HTML declares its data needs
// in a <script type="application/json" id="jarvis-sources"> block; the runtime
// parses it at boot to resolve jarvis.data("<name>") → {tool, spec}, and the
// parent parses the SAME block (parseSourcesBlock below) for the save-dialog
// preview + save payload.

export const CSP_META =
	'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data: blob:; font-src data:; connect-src \'none\'">'

// ── the bridge runtime (inline JS, stringified into the srcdoc) ───────────────
// window.jarvis API for dashboard HTML:
//   jarvis.data(name)        → Promise; `query`/`get_list` sources resolve with
//                              the rows array; `run_report` sources resolve
//                              with {columns, rows}. Rejections carry .code
//                              ("PermissionError"|"NotFound"|"Timeout"|...).
//   jarvis.ready()           → tells the parent boot finished (auto-posted on
//                              DOMContentLoaded too).
//   jarvis.renderError(el,e) → quiet inline per-widget error block.
// Frames OUT: {jarvis:1, v:1, type:"data"|"ready"|"height"|"export:result", ...}
// Frames IN (validated e.source === window.parent && d.jarvis === 1):
//   {type:"data:result", id, ok, rows|error} · {type:"theme", dark} ·
//   {type:"export", id, format:"png"|"slides", lib, pixelRatio}
export const RUNTIME_JS = `(function () {
	"use strict";
	var sources = {}; // name -> {tool, spec}
	var pending = {}; // data request id -> {resolve, reject, timer}
	var seq = 0;
	var exportLibInjected = false;

	function post(msg) {
		msg.jarvis = 1;
		msg.v = 1;
		window.parent.postMessage(msg, "*");
	}

	function parseSources() {
		var el = document.getElementById("jarvis-sources");
		if (!el) return;
		try {
			var parsed = JSON.parse(el.textContent || "{}");
			var list = (parsed && parsed.sources) || [];
			for (var i = 0; i < list.length; i++) {
				// LLM-authored blocks drift toward the tool-call surface; fold the
				// common dialects into the canonical {source_name, tool, spec} shape
				// (the backend save parser normalizes identically).
				var s = list[i];
				if (!s) continue;
				var name = s.source_name || s.id || s.name;
				if (!name) continue;
				var tool = String(s.tool || "").replace(/^jarvis__/, "");
				var spec = s.spec;
				if (spec == null && s.args && typeof s.args === "object") {
					spec = s.args.spec && typeof s.args.spec === "object" ? s.args.spec : s.args;
				}
				// double-wrap drift ({"spec":{"spec":{...}}}): unwrap lone spec keys
				while (
					spec && typeof spec === "object" && !Array.isArray(spec) &&
					Object.keys(spec).length === 1 && spec.spec && typeof spec.spec === "object"
				) {
					spec = spec.spec;
				}
				sources[name] = { tool: tool, spec: spec };
			}
		} catch (e) {
			/* malformed block: jarvis.data() rejects per-name with NotFound */
		}
	}

	// Dashboard widget scripts run inline DURING document parsing - often
	// before the #jarvis-sources block (or the rest of the DOM) exists. Defer
	// every lookup until the document is fully parsed, then re-parse lazily,
	// so widget/block ordering never matters.
	function whenParsed(fn) {
		if (document.readyState === "loading") {
			document.addEventListener("DOMContentLoaded", fn);
		} else {
			fn();
		}
	}

	window.jarvis = {
		data: function (name) {
			return new Promise(function (resolve, reject) {
				whenParsed(function () {
					var src = sources[name];
					if (!src) {
						parseSources();
						src = sources[name];
					}
					if (!src) {
						var known = Object.keys(sources).join(", ") || "none declared";
						var e = new Error('Unknown source "' + name + '" (declared: ' + known + ")");
						e.code = "NotFound";
						return reject(e);
					}
					var id = "d" + ++seq;
					var timer = setTimeout(function () {
						delete pending[id];
						var e = new Error("Timed out loading data");
						e.code = "Timeout";
						reject(e);
					}, 30000);
					pending[id] = { resolve: resolve, reject: reject, timer: timer };
					post({ type: "data", id: id, name: name, tool: src.tool, spec: src.spec });
				});
			});
		},
		ready: function () {
			post({ type: "ready" });
		},
		renderError: function (el, err) {
			if (!el) return;
			var msg =
				err && err.code === "PermissionError"
					? "No permission to view this data"
					: "Couldn't load this data";
			el.innerHTML = "";
			var d = document.createElement("div");
			d.textContent = msg;
			d.style.cssText =
				"font-family:system-ui,sans-serif;font-size:13px;opacity:.55;padding:12px;text-align:center;";
			el.appendChild(d);
		},
	};

	function captureOne(el, pixelRatio) {
		return window.htmlToImage
			.toPng(el, {
				pixelRatio: pixelRatio,
				skipFonts: true,
				backgroundColor: getComputedStyle(document.body).backgroundColor,
			})
			.then(function (dataUrl) {
				return {
					dataUrl: dataUrl,
					w: Math.round(el.offsetWidth * pixelRatio),
					h: Math.round(el.offsetHeight * pixelRatio),
					title: (el.dataset && el.dataset.title) || "",
				};
			});
	}

	function handleExport(d) {
		try {
			if (!exportLibInjected && d.lib) {
				var s = document.createElement("script");
				s.textContent = d.lib;
				document.head.appendChild(s);
				exportLibInjected = true;
			}
			if (!window.htmlToImage) throw new Error("capture library unavailable");
			var els =
				d.format === "slides"
					? Array.prototype.slice.call(document.querySelectorAll("section.slide"))
					: [];
			if (!els.length) els = [document.body];
			var pixelRatio = d.pixelRatio || 2;
			// Browsers cap canvases near 16384px a side - drop to 1x past that.
			for (var i = 0; i < els.length; i++) {
				var max = Math.max(els[i].offsetWidth, els[i].offsetHeight);
				if (max * pixelRatio > 16384) pixelRatio = 1;
			}
			// SEQUENTIAL captures: parallel toPng calls contend for layout/canvas.
			var images = [];
			var chain = Promise.resolve();
			els.forEach(function (el) {
				chain = chain.then(function () {
					return captureOne(el, pixelRatio).then(function (img) {
						images.push(img);
					});
				});
			});
			chain
				.then(function () {
					post({ type: "export:result", id: d.id, ok: true, images: images });
				})
				.catch(function (err) {
					post({
						type: "export:result",
						id: d.id,
						ok: false,
						error: { code: "CaptureFailed", message: String((err && err.message) || err) },
					});
				});
		} catch (err) {
			post({
				type: "export:result",
				id: d.id,
				ok: false,
				error: { code: "CaptureFailed", message: String((err && err.message) || err) },
			});
		}
	}

	window.addEventListener("message", function (e) {
		if (e.source !== window.parent) return;
		var d = e.data;
		if (!d || d.jarvis !== 1) return;
		if (d.type === "data:result") {
			var p = pending[d.id];
			if (!p) return;
			delete pending[d.id];
			clearTimeout(p.timer);
			if (d.ok) p.resolve(d.rows);
			else {
				var err = new Error((d.error && d.error.message) || "Couldn't load this data");
				err.code = (d.error && d.error.code) || "InternalError";
				p.reject(err);
			}
		} else if (d.type === "theme") {
			document.documentElement.dataset.theme = d.dark ? "dark" : "light";
			window.dispatchEvent(new CustomEvent("jarvis:theme", { detail: { dark: !!d.dark } }));
		} else if (d.type === "export") {
			handleExport(d);
		}
	});

	function boot() {
		parseSources();
		if (typeof ResizeObserver !== "undefined" && document.body) {
			new ResizeObserver(function () {
				post({ type: "height", height: document.body.scrollHeight });
			}).observe(document.body);
		}
		post({ type: "ready" });
	}
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
	else boot();
})();`

// Any JS inlined into HTML must not terminate its own <script> wrapper.
function escInline(js) {
	return String(js || "").replace(/<\/script/gi, "<\\/script")
}

// Split author HTML into (headInner, bodyInner). We NEVER re-emit the author's
// own <html>/<head>/<body> tags or anything before them — see buildSrcdoc for
// why (CSP-ordering: markup before <head> would execute pre-CSP). Anything
// outside the first <head> and the <body> (e.g. a <script> placed before
// <head>) is intentionally DROPPED, not run.
function splitAuthorHtml(src) {
	const headM = /<head[^>]*>([\s\S]*?)<\/head>/i.exec(src)
	const bodyM = /<body[^>]*>([\s\S]*?)<\/body>/i.exec(src)
	if (bodyM) {
		return { headInner: headM ? headM[1] : "", bodyInner: bodyM[1] }
	}
	if (headM) {
		// <head> but no <body>: body is everything after </head>, minus a
		// trailing </html>.
		const after = src
			.slice(headM.index + headM[0].length)
			.replace(/<\/html>\s*$/i, "")
		return { headInner: headM[1], bodyInner: after }
	}
	// fragment (no head/body): strip a leading doctype + any stray html tags,
	// everything is body content.
	const body = src
		.replace(/^\s*<!doctype[^>]*>\s*/i, "")
		.replace(/<\/?html[^>]*>/gi, "")
	return { headInner: "", bodyInner: body }
}

// buildSrcdoc(html, {dark, echartsSource, theme}) → the full srcdoc string.
// Sync + pure: the caller dynamic-imports echarts.min.js?raw only when the
// html references echarts, so the 1MB source never rides the main chunk.
//
// SECURITY — CSP must be the FIRST thing the parser sees. We ALWAYS emit our
// own <!DOCTYPE html><html><head>CSP+scripts+…</head><body>author</body>
// shell and only ever place author content INSIDE <head>/<body>, extracted
// via splitAuthorHtml. We never reuse the author's <html>/<head>/<body> tags.
// (Injecting CSP *inside* an author's existing <head> is unsafe: a <script>
// placed before that <head> — e.g. `<html><script>evil</script><head>…` —
// executes during "before head" parsing, BEFORE the CSP meta, and could open
// a socket that survives connect-src 'none' and exfiltrate another viewer's
// bridged data. Wrapping guarantees nothing author-supplied precedes the CSP.)
export function buildSrcdoc(
	html,
	{ dark = false, echartsSource = "", theme: themeSpec = null } = {},
) {
	// A named dashboard theme (from lib/dashboardThemes) owns the canvas look
	// when provided: its --jd-* variables + named component classes are emitted
	// in a later-declared `@layer theme` while the author's own <style> is
	// wrapped in `@layer author` (declared first), so the theme wins the CSS-vs-
	// CSS battle regardless of author source order or specificity. A JARVIS_THEME
	// global exposes the chart palette, and data-theme follows the theme's own
	// light/dark - independent of the app shell's mode. A "Custom" (bespoke)
	// theme is the opposite: the tokens are injected UNLAYERED and the author's
	// <style> is left unlayered too, so the author's own design wins (the save-
	// time validator relaxes the design rules for it but keeps the safety bans).
	// Without a theme the legacy dark flag drives data-theme only.
	const theme = themeSpec ? (themeSpec.dark ? "dark" : "light") : dark ? "dark" : "light"
	const bespoke = !!(themeSpec && themeSpec.bespoke)
	const themeCss = themeSpec
		? bespoke
			? themeSpec.css
			: `@layer author, theme;@layer theme{${themeSpec.css}}`
		: ""
	const themeBlock = themeSpec
		? `<style id="jarvis-theme">${themeCss}</style>` +
			`<script>window.JARVIS_THEME=${escInline(
				JSON.stringify({
					name: themeSpec.key,
					label: themeSpec.label,
					dark: !!themeSpec.dark,
					accent: themeSpec.accent,
					palette: themeSpec.palette,
				}),
			)}<\/script>`
		: ""
	const scripts =
		themeBlock +
		(echartsSource ? `<script>${escInline(echartsSource)}<\/script>` : "") +
		`<script>${escInline(RUNTIME_JS)}<\/script>`

	const { headInner, bodyInner } = splitAuthorHtml(String(html || ""))
	// Under a standard (non-bespoke) theme, wrap the author's <style> contents in
	// `@layer author` so the theme's `@layer theme` wins. Skipped for bespoke /
	// no-theme, where author CSS stays unlayered and wins.
	const prep = (s) =>
		themeSpec && !bespoke ? wrapAuthorStyles(stripHostClient(s)) : stripHostClient(s)
	return (
		`<!DOCTYPE html><html data-theme="${theme}"><head>` +
		CSP_META +
		'<meta charset="utf-8">' +
		scripts +
		prep(headInner) +
		"</head><body>" +
		prep(bodyInner) +
		"</body></html>"
	)
}

// Wrap the inner CSS of every author <style> in `@layer author { … }` so a
// standard theme's later-declared `@layer theme` wins the cascade regardless of
// the author's source order or selector specificity. Non-greedy to the first
// </style>; an empty style block is left untouched. Author `@import`, inline
// `style=` colors and `!important` on color/font still beat layers by design —
// the save-time validator bans exactly those.
function wrapAuthorStyles(html) {
	return String(html || "").replace(
		/(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi,
		(m, open, css, close) => (css.trim() ? `${open}@layer author{${css}}${close}` : m),
	)
}

// Dashboards saved before the gateway host-client strip landed carry a dead
// openclaw live-reload script (a WebSocket to /__openclaw__/ws). It can't work
// inside the sandbox (connect-src 'none' blocks it) and just logs a CSP
// violation on every view — drop any <script> that references the host socket.
function stripHostClient(html) {
	return String(html || "").replace(/<script\b[\s\S]*?<\/script>/gi, (m) =>
		m.includes("__openclaw__/ws") ? "" : m,
	)
}

// Parent-side parse of the SAME #jarvis-sources block the runtime reads -
// feeds the save dialog's detected-sources preview and the save payload.
// String-based (no DOM) so it is testable and works on the raw html prop.
// Normalizes the same LLM dialects as the runtime (id/name for source_name,
// jarvis__ tool prefix, args/args.spec for spec) so preview, save and render
// always agree. → [{source_name, tool, spec}]
export function parseSourcesBlock(html) {
	const m =
		/<script[^>]*\bid\s*=\s*["']jarvis-sources["'][^>]*>([\s\S]*?)<\/script>/i.exec(
			String(html || ""),
		)
	if (!m) return []
	try {
		const parsed = JSON.parse(m[1])
		const list = (parsed && parsed.sources) || []
		return list
			.map((s) => {
				if (!s) return null
				const source_name = s.source_name || s.id || s.name
				if (!source_name) return null
				const tool = String(s.tool || "").replace(/^jarvis__/, "")
				let spec = s.spec
				if (spec == null && s.args && typeof s.args === "object") {
					spec = s.args.spec && typeof s.args.spec === "object" ? s.args.spec : s.args
				}
				// double-wrap drift ({"spec":{"spec":{...}}}): unwrap lone spec keys
				while (
					spec &&
					typeof spec === "object" &&
					!Array.isArray(spec) &&
					Object.keys(spec).length === 1 &&
					spec.spec &&
					typeof spec.spec === "object"
				) {
					spec = spec.spec
				}
				return { source_name, tool, spec }
			})
			.filter(Boolean)
	} catch (e) {
		return []
	}
}
