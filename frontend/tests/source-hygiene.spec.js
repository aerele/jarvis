// Static source-hygiene guard (vitest). Scans every src/**/*.{vue,js} AND
// tests/**/*.{vue,js} for stray C0/C1 control characters (excluding the
// legitimate whitespace tab/LF/CR). Tests are scanned too so this guard covers
// itself and its neighbours, not only app code.
//
// This exists because an invisible U+0001 once slipped in immediately before a
// `class=` attribute in a .vue template. Vue's compiler accepted it silently and
// the build passed, but the rendered attribute name became "class", which
// jsdom (and a real browser's setAttribute) reject with
// `"class" did not match the Name production`. Nothing caught it until a mounted
// component test blew up far from the cause. A byte-level scan catches the whole
// class of invisible-corruption bugs at their source, cheaply.
import fs from "node:fs";
import path from "node:path";

// vitest runs with cwd at the frontend root (where package.json lives). Both
// roots are relative to it; `rel` below is reported from the frontend root so a
// src/ and a tests/ hit read the same way.
const ROOT = process.cwd();
const SCAN_DIRS = [path.join(ROOT, "src"), path.join(ROOT, "tests")];

// Control chars that must never appear in source: C0 range except tab (\x09),
// LF (\x0A), CR (\x0D); plus DEL (\x7F) and the C1 range (\x80-\x9F). A global
// regex so we can report every offending position, not just the first.
const CONTROL_RE = /[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\x80-\x9F]/g;

function walk(dir, acc = []) {
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) walk(full, acc);
		else if (/\.(vue|js)$/.test(entry.name)) acc.push(full);
	}
	return acc;
}

describe("source hygiene guard", () => {
	const files = SCAN_DIRS.flatMap((d) => walk(d));

	it("scans a non-trivial number of source files (sanity)", () => {
		expect(files.length).toBeGreaterThan(20);
	});

	it("has no stray control characters in any .vue/.js source", () => {
		const violations = [];
		for (const file of files) {
			const rel = path.relative(ROOT, file).replace(/\\/g, "/");
			const src = fs.readFileSync(file, "utf8");
			const lines = src.split("\n");
			lines.forEach((line, i) => {
				for (const m of line.matchAll(CONTROL_RE)) {
					const code = m[0].codePointAt(0).toString(16).padStart(4, "0");
					violations.push(
						`${rel}:${i + 1} :: U+${code.toUpperCase()} at col ${m.index + 1}`
					);
				}
			});
		}
		expect(
			violations,
			`Stray control characters found in source (invisible; corrupt attribute ` +
				`names and string literals). Remove them:\n  ${violations.join("\n  ")}`
		).toEqual([]);
	});
});
