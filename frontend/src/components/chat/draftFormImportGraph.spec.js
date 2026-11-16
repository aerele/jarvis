// The draft edit form is shared by the desktop SPA and the phone PWA, which resolve `@/`
// to different folders and have no frappe-ui, router or store in common. So everything
// the form pulls in must be reachable by relative path alone. A single `@/` import in
// any file of the graph builds on the desktop and breaks the phone's build, so this walks
// the import graph from the form's files and fails on the first one that leaves it.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "../..");
const here = (...p) => path.resolve(__dirname, ...p);

// The form the phone will mount, then each of its parts on its own (so a part stays clean even
// if the form stops using it), and the desktop-only preview, which shares the libs.
const ENTRIES = [
	here("DraftEditForm.vue"),
	here("DraftCell.vue"),
	here("DraftLinkMenu.vue"),
	here("DraftRowBlock.vue"),
	here("DraftRowForm.vue"),
	here("../doc/DraftPreview.vue"),
	// The link-search state the phone sheet will call with its own search function.
	here("../../lib/useDraftLink.js"),
];
// The only package a file in the graph may import.
const ALLOWED_PACKAGES = new Set(["vue"]);
// Relative imports that reach app singletons (router, stores, api client, entry point).
const FORBIDDEN_AREAS = [/^(stores?|router|api|main)(\/|\.|$)/];

const STATIC_IMPORT =
	/(?:^|[\n;])\s*(?:import|export)\s+(?:[^"'`;]*?\s+from\s+)?["']([^"']+)["']/g;
const DYNAMIC_IMPORT = /\bimport\(\s*["']([^"']+)["']\s*\)/g;

function scriptOf(file, text) {
	if (!file.endsWith(".vue")) return text;
	return [...text.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join("\n");
}

function specifiers(text) {
	const code = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
	return [...code.matchAll(STATIC_IMPORT), ...code.matchAll(DYNAMIC_IMPORT)].map((m) => m[1]);
}

function resolveRelative(from, spec) {
	const base = path.resolve(path.dirname(from), spec);
	for (const candidate of [base, `${base}.js`, `${base}.vue`, path.join(base, "index.js")]) {
		if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
	}
	return null;
}

// Every problem found, as "<file>: <why>", walking breadth-first from the entries.
function walk(entries) {
	const problems = [];
	const seen = new Set();
	const queue = [...entries];
	while (queue.length) {
		const file = queue.shift();
		if (seen.has(file)) continue;
		seen.add(file);
		const rel = path.relative(SRC, file);
		if (!fs.existsSync(file)) {
			problems.push(`${rel}: file not found`);
			continue;
		}
		const text = scriptOf(file, fs.readFileSync(file, "utf8"));
		for (const spec of specifiers(text)) {
			if (spec.startsWith("@/") || spec.startsWith("@shared/")) {
				problems.push(
					`${rel}: imports "${spec}" (an alias is a different folder on the phone)`
				);
			} else if (spec.startsWith(".")) {
				const target = resolveRelative(file, spec);
				if (!target) {
					problems.push(`${rel}: relative import "${spec}" does not resolve`);
					continue;
				}
				const fromSrc = path.relative(SRC, target);
				if (fromSrc.startsWith("..")) {
					problems.push(`${rel}: "${spec}" leaves src/`);
				} else if (FORBIDDEN_AREAS.some((re) => re.test(fromSrc))) {
					problems.push(`${rel}: "${spec}" reaches an app singleton (${fromSrc})`);
				} else {
					queue.push(target);
				}
			} else if (!ALLOWED_PACKAGES.has(spec)) {
				// frappe-ui, vue-router, pinia, anything bare that is not vue
				problems.push(`${rel}: imports package "${spec}" (the phone does not share it)`);
			}
		}
	}
	return { problems, seen };
}

describe("the draft edit form imports nothing the phone cannot resolve", () => {
	it("walks from every entry, reaches the shared libs, and finds no alias, package or singleton", () => {
		const { problems, seen } = walk(ENTRIES);
		expect(problems).toEqual([]);
		// The walk really descended: the libs the form is built on were visited.
		const names = [...seen].map((f) => path.basename(f));
		for (const n of [
			"draftApply.js",
			"draftTables.js",
			"rowForm.js",
			"docFields.js",
			"rowStatusMemo.js",
			"draftTotals.js",
			"rowFormFocus.js",
			"draftLinkFilters.js", // reached from useDraftLink.js
		]) {
			expect(names, `${n} was not reached`).toContain(n);
		}
	});

	it("reports an alias, a package and a singleton when a file has one (the walker can fail)", () => {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "import-graph-"));
		try {
			const bad = path.join(dir, "bad.js");
			fs.writeFileSync(
				bad,
				[
					'import { a } from "@/lib/x";',
					'import { Button } from "frappe-ui";',
					'import { useRouter } from "vue-router";',
					'import { ref } from "vue";',
					'export { y } from "@/lib/y";',
					'const z = () => import("pinia");',
				].join("\n")
			);
			const { problems } = walk([bad]);
			expect(problems).toHaveLength(5);
			expect(problems.join("\n")).toMatch(/"@\/lib\/x"/);
			expect(problems.join("\n")).toMatch(/"@\/lib\/y"/);
			expect(problems.join("\n")).toMatch(/package "frappe-ui"/);
			expect(problems.join("\n")).toMatch(/package "vue-router"/);
			expect(problems.join("\n")).toMatch(/package "pinia"/);
		} finally {
			fs.rmSync(dir, { recursive: true, force: true });
		}
	});
});
