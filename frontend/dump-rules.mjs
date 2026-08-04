import { readFileSync } from "node:fs";
import postcss from "postcss";

const css = readFileSync("src/assets/settings.css", "utf8");
const root = postcss.parse(css, { from: "src/assets/settings.css" });

function splitSelectorList(selector) {
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

root.walkRules((rule) => {
	const selectors = splitSelectorList(rule.selector);
	const classesPerSelector = selectors.map((sel) => {
		const found = sel.match(/\.jv-[a-zA-Z0-9_-]+/g) || [];
		return found.map((c) => c.slice(1));
	});
	const parentType = rule.parent && rule.parent.type === "atrule" ? `@${rule.parent.name} ${rule.parent.params}` : "";
	console.log(
		JSON.stringify({
			line: rule.source.start.line,
			endLine: rule.source.end.line,
			selector: rule.selector,
			selectors,
			classesPerSelector,
			parent: parentType,
		})
	);
});
