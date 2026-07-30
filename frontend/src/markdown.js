// Compact GFM renderer - enough for agent replies: paragraphs, bold/italic,
// strikethrough, inline code, links, nested bullet/number lists, blockquotes,
// and pipe tables (rendered into the imported design's table look via the
// .jv-md classes in ChatView's styles).
function esc(s) {
	return String(s).replace(
		/[&<>"]/g,
		(c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
	);
}

function inline(s) {
	let t = esc(s);
	// esc() above escaped ALL html (XSS-safe for LLM output). Re-permit ONLY a
	// bare, attribute-less <br> (also <br/> and <br />): it's the standard way to
	// line-break inside a GFM table cell, which agents rely on, and it's inert —
	// no attributes means no script/handler/URL surface. The attribute form
	// (&lt;br onload=...&gt;) deliberately stays escaped, so the safe posture holds.
	t = t.replace(/&lt;br\s*\/?&gt;/gi, "<br>");
	t = t.replace(/`([^`]+)`/g, '<code class="jv-md-code">$1</code>');
	t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");
	t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
	t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
	t = t.replace(
		/\[([^\]]+)\]\((https?:[^)]+)\)/g,
		'<a href="$2" target="_blank" rel="noopener" class="jv-md-link">$1</a>'
	);
	return t;
}

function renderTable(rows) {
	const cells = (r) =>
		r
			.replace(/^\||\|$/g, "")
			.split("|")
			.map((c) => c.trim());
	const head = cells(rows[0]);
	const aligns = cells(rows[1]).map((s) =>
		/^:-+:$/.test(s) ? "center" : /-+:$/.test(s) ? "right" : "left"
	);
	const body = rows.slice(2).map(cells);
	let h = '<div class="jv-md-tablewrap"><table class="jv-md-table"><thead><tr>';
	head.forEach(
		(c, i) => (h += `<th style="text-align:${aligns[i] || "left"}">${inline(c)}</th>`)
	);
	h += "</tr></thead><tbody>";
	body.forEach((r) => {
		h += "<tr>";
		r.forEach(
			(c, i) => (h += `<td style="text-align:${aligns[i] || "left"}">${inline(c)}</td>`)
		);
		h += "</tr>";
	});
	return h + "</tbody></table></div>";
}

// Build (possibly nested) list HTML from a run of list-item lines, nesting by
// leading-space indent. A deeper indent nests inside the current item; a mixed
// bullet/number type at the same indent starts a fresh sibling list.
function renderListBlock(items) {
	let html = "";
	const stack = []; // [{ indent, tag }], innermost last
	for (const it of items) {
		while (stack.length && it.indent < stack[stack.length - 1].indent) {
			html += `</li></${stack.pop().tag}>`;
		}
		const top = stack[stack.length - 1];
		if (top && it.indent === top.indent) {
			if (top.tag !== it.tag) {
				html += `</li></${stack.pop().tag}>`;
				html += `<${it.tag} class="jv-md-list"><li>`;
				stack.push({ indent: it.indent, tag: it.tag });
			} else {
				html += "</li><li>";
			}
		} else {
			html += `<${it.tag} class="jv-md-list"><li>`;
			stack.push({ indent: it.indent, tag: it.tag });
		}
		html += inline(it.text);
	}
	while (stack.length) html += `</li></${stack.pop().tag}>`;
	return html;
}

export function renderMarkdown(src) {
	if (!src) return "";
	const lines = String(src).replace(/\r\n/g, "\n").split("\n");
	const out = [];
	let i = 0;
	let para = [];
	const flushPara = () => {
		if (para.length) {
			out.push(`<p class="jv-md-p">${inline(para.join(" "))}</p>`);
			para = [];
		}
	};
	while (i < lines.length) {
		const line = lines[i];
		// fenced code block: ``` or ```lang - mermaid renders as a diagram,
		// everything else as a styled code block.
		const fence = line.match(/^\s*```\s*([\w-]*)\s*$/);
		if (fence) {
			flushPara();
			const lang = (fence[1] || "").toLowerCase();
			const body = [];
			i++;
			while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
			i++; // consume closing fence
			const code = body.join("\n");
			if (lang === "mermaid") {
				out.push(`<div class="jv-mermaid">${esc(code)}</div>`);
			} else {
				out.push(`<pre class="jv-md-pre"><code>${esc(code)}</code></pre>`);
			}
			continue;
		}
		// table: a header row followed by a |---| separator
		if (
			/\|/.test(line) &&
			i + 1 < lines.length &&
			/^\s*\|?[\s:-]+\|[\s:|-]*$/.test(lines[i + 1])
		) {
			flushPara();
			const tbl = [line, lines[i + 1]];
			i += 2;
			while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim())
				tbl.push(lines[i++]);
			out.push(renderTable(tbl));
			continue;
		}
		// ATX headings (#-####): wiki page bodies lead with them, and a
		// knowledge base that shows raw hashes reads as broken.
		const heading = line.match(/^\s*(#{1,4})\s+(.*)/);
		if (heading) {
			flushPara();
			const level = Math.min(heading[1].length + 2, 6);
			out.push(`<h${level} class="jv-md-h">${inline(heading[2])}</h${level}>`);
			i++;
			continue;
		}
		// blockquote: one or more consecutive `>` lines.
		if (/^\s*>\s?/.test(line)) {
			flushPara();
			const q = [];
			while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
				q.push(lines[i].replace(/^\s*>\s?/, ""));
				i++;
			}
			out.push(`<blockquote class="jv-md-quote">${inline(q.join(" "))}</blockquote>`);
			continue;
		}
		// bullet / numbered lists, indent-nested.
		if (/^(\s*)([-*]|\d+\.)\s+/.test(line)) {
			flushPara();
			const items = [];
			while (i < lines.length) {
				const m = lines[i].match(/^(\s*)([-*]|\d+\.)\s+(.*)/);
				if (m) {
					items.push({
						indent: m[1].length,
						tag: /\d/.test(m[2]) ? "ol" : "ul",
						text: m[3],
					});
					i++;
					continue;
				}
				// A blank line inside a list (a "loose" list, common in agent
				// output) does NOT end it: keep the same list only if another
				// list item follows. A real paragraph after the blank ends it,
				// so "1. a\n\n2. b" is ONE <ol> (1, 2), not two that both show 1.
				if (!lines[i].trim()) {
					let j = i + 1;
					while (j < lines.length && !lines[j].trim()) j++;
					if (j < lines.length && /^(\s*)([-*]|\d+\.)\s+/.test(lines[j])) {
						i = j;
						continue;
					}
				}
				break;
			}
			out.push(renderListBlock(items));
			continue;
		}
		if (!line.trim()) {
			flushPara();
			i++;
			continue;
		}
		para.push(line.trim());
		i++;
	}
	flushPara();
	return out.join("\n");
}
