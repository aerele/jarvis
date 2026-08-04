#!/usr/bin/env node
// Usage scan for .jv-* selectors defined in frontend/src/assets/settings.css.
//
// Strategy:
// 1. Parse settings.css with postcss to get every top-level and nested rule's
//    selectors structurally (comments, media queries, @keyframes handled by
//    the parser, not by regex).
// 2. Extract every distinct `.jv-xxxx` class token referenced by those
//    selectors (ignoring combinators, pseudo-classes, attribute selectors).
// 3. Search the rest of the frontend source tree (excluding settings.css
//    itself) for any textual reference to each class name: class="...",
//    :class bindings, template literals, JS string literals. This is a
//    generic substring search per class name across all source files, plus
//    a separate check for each file's own <style> blocks so a class that
//    only appears inside another component's *own* <style> (i.e. redefined
//    there, not consumed) is not miscounted as "used".
// 4. Report classes with zero references outside settings.css and outside
//    any other file's own <style> block.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, extname, relative } from "node:path";
import postcss from "postcss";

const FRONTEND_SRC = process.argv[2] || "src";
const CSS_FILE = process.argv[3] || "src/assets/settings.css";

// ---------- Step 1+2: extract selectors from settings.css ----------

function extractClassesFromCss(cssPath) {
	const css = readFileSync(cssPath, "utf8");
	const root = postcss.parse(css, { from: cssPath });
	const classes = new Set();
	const classToRuleSelectors = new Map(); // class -> Set of full selectors it appeared in

	root.walkRules((rule) => {
		// rule.selector may be a comma list; postcss gives raw selector string.
		// Split on top-level commas (postcss selectors don't nest commas inside
		// parens in our file, but guard simple parens like :not()).
		const selectors = splitSelectorList(rule.selector);
		for (const sel of selectors) {
			const found = sel.match(/\.jv-[a-zA-Z0-9_-]+/g) || [];
			for (const cls of found) {
				const name = cls.slice(1); // strip leading dot
				classes.add(name);
				if (!classToRuleSelectors.has(name)) classToRuleSelectors.set(name, new Set());
				classToRuleSelectors.get(name).add(sel.trim());
			}
		}
	});

	// Also catch classes referenced only inside @keyframes-adjacent constructs
	// or animation-name / transition-name-derived class families is handled
	// separately below (runtime-derived names), not here.

	return { classes, classToRuleSelectors };
}

function splitSelectorList(selector) {
	// naive top-level comma split, respecting parens
	const parts = [];
	let depth = 0;
	let cur = "";
	for (const ch of selector) {
		if (ch === "(") depth++;
		if (ch === ")") depth--;
		if (ch === "," && depth === 0) {
			parts.push(cur);
			cur = "";
		} else {
			cur += ch;
		}
	}
	if (cur.trim()) parts.push(cur);
	return parts;
}

// ---------- Step 3: walk source files ----------

const SKIP_DIRS = new Set(["node_modules", "dist", ".git", "coverage"]);
const TEXT_EXT = new Set([".vue", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".md"]);

function walk(dir, out = []) {
	for (const entry of readdirSync(dir)) {
		if (SKIP_DIRS.has(entry)) continue;
		const full = join(dir, entry);
		const st = statSync(full);
		if (st.isDirectory()) {
			walk(full, out);
		} else if (TEXT_EXT.has(extname(full))) {
			out.push(full);
		}
	}
	return out;
}

// Extract a file's own <style> block contents (for .vue SFCs) so we can
// exclude "defined in its own style block elsewhere" matches from counting
// as usage.
function ownStyleBlockRanges(content) {
	const ranges = [];
	const re = /<style[^>]*>([\s\S]*?)<\/style>/gi;
	let m;
	while ((m = re.exec(content))) {
		ranges.push([m.index, m.index + m[0].length]);
	}
	return ranges;
}

function isInsideRanges(idx, ranges) {
	return ranges.some(([a, b]) => idx >= a && idx < b);
}

function main() {
	const cssAbsPath = CSS_FILE;
	const { classes, classToRuleSelectors } = extractClassesFromCss(cssAbsPath);

	const allFiles = walk(FRONTEND_SRC);
	const otherFiles = allFiles.filter((f) => relative(".", f) !== relative(".", cssAbsPath));

	const fileContents = new Map();
	for (const f of otherFiles) {
		fileContents.set(f, readFileSync(f, "utf8"));
	}

	const results = [];
	for (const cls of [...classes].sort()) {
		const usages = []; // {file, line, kind}
		for (const [f, content] of fileContents) {
			const isVue = extname(f) === ".vue";
			const styleRanges = isVue ? ownStyleBlockRanges(content) : [];
			// find all occurrences of the literal class name as a "word" (not
			// part of a longer class name), e.g. jv-btn should not match
			// jv-btn-danger.
			const re = new RegExp(`(?<![\\w-])${escapeRegex(cls)}(?![\\w-])`, "g");
			let m;
			while ((m = re.exec(content))) {
				const inOwnStyle = isInsideRanges(m.index, styleRanges);
				const lineNum = content.slice(0, m.index).split("\n").length;
				usages.push({
					file: relative(".", f),
					line: lineNum,
					inOwnStyle,
				});
			}
		}

		const realUsages = usages.filter((u) => !u.inOwnStyle);
		const onlyInOtherStyleBlocks = usages.length > 0 && realUsages.length === 0;

		results.push({
			class: cls,
			selectors: [...classToRuleSelectors.get(cls)],
			totalMatches: usages.length,
			realUsageCount: realUsages.length,
			realUsages: realUsages.slice(0, 5),
			onlyInOtherStyleBlocks,
		});
	}

	const dead = results.filter((r) => r.realUsageCount === 0);
	const live = results.filter((r) => r.realUsageCount > 0);

	console.log(`Total unique .jv-* classes defined in ${CSS_FILE}: ${results.length}`);
	console.log(`Live (referenced outside settings.css, outside other files' own <style>): ${live.length}`);
	console.log(`Dead (zero genuine reference): ${dead.length}`);
	console.log("");
	console.log("=== DEAD CLASSES ===");
	for (const r of dead) {
		const note = r.onlyInOtherStyleBlocks ? "  [only matched inside another file's own <style> block]" : "";
		console.log(`- ${r.class}${note}`);
	}
	console.log("");
	console.log("=== LIVE CLASSES (with first usage) ===");
	for (const r of live) {
		const u = r.realUsages[0];
		console.log(`- ${r.class}  (${r.realUsageCount}x, e.g. ${u.file}:${u.line})`);
	}
}

function escapeRegex(s) {
	return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

main();
