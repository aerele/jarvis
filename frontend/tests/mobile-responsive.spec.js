// Static mobile-safety guard (vitest). Scans every src/**/*.vue for a small
// set of class-level anti-patterns that break phone layouts, so a future UI
// change that reintroduces one fails here instead of shipping. It is heuristic
// (no real layout) — the Playwright pass in tests/e2e/mobile.spec.js catches
// actual overflow. The two layers back each other up.
//
// When a NEW match is legitimate (e.g. a fixed width that genuinely lives
// inside its own overflow-x scroller), either wrap the cause so the pattern no
// longer matches, or add a precise "<relpath> :: <rule> :: <match>" line to
// ALLOWLIST below with a comment saying why. Keep the allowlist tiny.
import fs from "node:fs";
import path from "node:path";

// vitest runs with cwd at the frontend root (where package.json lives).
const SRC = path.resolve(process.cwd(), "src");

// Known, deliberate exceptions. Format: "<relpath> :: <rule> :: <match>".
// Empty by design — the codebase is clean today; keep it that way.
const ALLOWLIST = new Set([]);

// Each rule returns the offending substrings for a file's source (empty = ok).
const RULES = {
	// 100vh height with no dynamic-viewport companion: the on-screen keyboard
	// and mobile browser chrome then cover the bottom of the screen (composer,
	// primary actions). Use h-dvh-safe / a `dvh` height instead.
	"h-screen-without-dvh": (src) => {
		if (/dvh/.test(src)) return [];
		return matchAll(src, /(?:^|["'\s])((?:min-)?h-screen)(?=["'\s])/g);
	},
	// 100vw forces content to the full viewport width incl. the scrollbar gutter,
	// a classic source of a horizontal scrollbar. Use w-full.
	"w-screen": (src) => matchAll(src, /(?:^|["'\s])(w-screen)(?=["'\s])/g),
	// Fixed pixel widths wider than a small phone (> 480px) overflow unless they
	// live in their own horizontal scroller. max-w-* is fine (it only caps).
	"fixed-width-over-480px": (src) =>
		matchAll(src, /(?:^|["'\s])((?:min-)?w-\[(\d+)px\])/g).filter(
			(m) => Number(m.match(/(\d+)px/)[1]) > 480
		),
	// A bare table overflows narrow screens; it must sit inside an overflow-x
	// (or overflow-auto) scroller. Coarse per-file check: table present, no
	// horizontal scroller anywhere in the file.
	"table-without-overflow-scroller": (src) => {
		if (!/<table[\s>]/.test(src)) return [];
		if (/overflow-(?:x|auto)/.test(src) || /overflow-x\s*:/.test(src)) return [];
		return ["<table>"];
	},
};

function matchAll(src, re) {
	return [...src.matchAll(re)].map((m) => m[1]);
}

function walkVue(dir, acc = []) {
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) walkVue(full, acc);
		else if (entry.name.endsWith(".vue")) acc.push(full);
	}
	return acc;
}

describe("mobile responsiveness guard", () => {
	const files = walkVue(SRC);

	it("scans a non-trivial number of components (sanity)", () => {
		expect(files.length).toBeGreaterThan(20);
	});

	it("has no un-allowlisted mobile anti-patterns", () => {
		const violations = [];
		for (const file of files) {
			const rel = path.relative(path.dirname(SRC), file).replace(/\\/g, "/");
			const src = fs.readFileSync(file, "utf8");
			for (const [rule, fn] of Object.entries(RULES)) {
				for (const match of fn(src)) {
					const key = `${rel} :: ${rule} :: ${match}`;
					if (!ALLOWLIST.has(key)) violations.push(key);
				}
			}
		}
		expect(
			violations,
			`Mobile anti-patterns found. Fix the layout, or (if truly intentional) ` +
				`add the exact key to ALLOWLIST in this file:\n  ${violations.join("\n  ")}`
		).toEqual([]);
	});
});
