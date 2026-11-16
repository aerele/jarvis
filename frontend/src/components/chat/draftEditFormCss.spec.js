// Scoped CSS is the one thing a unit test cannot see: when the draft edit form's body moved out
// of ChatView, every rule styling it had to move with it, or the form renders unstyled while
// every behaviour test stays green. These source checks guard that move. Layout itself is not
// measurable in jsdom; a browser comparison of the panel covers it.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const read = (...p) => fs.readFileSync(path.resolve(__dirname, ...p), "utf8");
const form = read("DraftEditForm.vue");
const view = read("../../views/ChatView.vue");

const block = (src, open, close, label) => {
	const i = src.indexOf(open);
	expect(i, `${label}: ${open} not found`).toBeGreaterThan(-1);
	const j = src.indexOf(close, i);
	expect(j, `${label}: ${close} not found`).toBeGreaterThan(-1);
	return src.slice(i, j);
};
const formTemplate = block(form, "<template>", "</template>\n\n<script", "form template");
const formStyleTag = form.slice(form.indexOf("<style"));
const formCss = block(form, "<style scoped>", "</style>", "form style").replace(
	"<style scoped>",
	""
);
// ChatView's own template is the first block; its style is the last.
const viewTemplate = block(view, "<template>", "\n</template>\n\n<script setup>", "view template");
// Everything in ChatView that is not its style block: the template and the script (a class can be
// built in a string there).
const viewNonStyle = view.slice(0, view.indexOf("<style scoped>"));
const viewCss = block(view, "<style scoped>", "</style>", "view style").replace(
	"<style scoped>",
	""
);

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");
// The selector text of every rule (everything before a `{`), comments removed.
const selectors = (css) =>
	[...stripComments(css).matchAll(/([^{}]+)\{/g)].map((m) => m[1].trim()).filter(Boolean);
const classesIn = (selector) => [...selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]);

// Every class the form's template uses: class="a b" and the keys of :class="{ a: x, 'b-c': y }".
function templateClasses(tpl) {
	const out = new Set();
	for (const m of tpl.matchAll(/\sclass="([^"]*)"/g))
		for (const c of m[1].split(/\s+/)) if (c) out.add(c);
	for (const m of tpl.matchAll(/:class="([\s\S]*?)"\s*(?:>|\n|\/|\w)/g))
		for (const k of m[1].matchAll(/(?:['"]([\w-]+)['"]|\b([A-Za-z_][\w-]*))\s*:/g))
			out.add(k[1] || k[2]);
	return out;
}
const used = templateClasses(formTemplate);
const wordIn = (text, token) => new RegExp(`(^|[^\\w-])${token}([^\\w-]|$)`).test(text);

// Classes the template uses that have NO rule of their own, and why that is right.
const NO_RULE_ALLOWED = {
	"jv-grid-openrow": "the open row's <tr>: a hook for tests and e2e; the card inside is styled",
	"jv-draft-table": "wraps one table's title, grid and Add row: never had a rule of its own",
};
// ChatView's rules for the shared input look. Nothing in ChatView's own template uses them any
// more (the form was their last user there), but they were COPIED into the form and the originals
// left in place, as agreed; they are the one thing allowed to stay for a class in the moved template.
const KEPT_IN_VIEW = new Set(["jv-action-input", "jv-action-sel"]);
const SHELL_CLASSES = [
	"jv-draft-panel",
	"jv-draft-panel-resizing",
	"jv-draft-resizer",
	"jv-draft-badge",
	"jv-draft-foot",
	"jv-artifact-panel",
	"jv-artifact-overlay",
	"jv-artifact-head",
	"jv-art-close",
];

describe("DraftEditForm carries its own styles", () => {
	it("extracts the classes the template really uses (the checks below are not vacuous)", () => {
		for (const c of [
			"jv-draft-body",
			"jv-grid-lead",
			"jv-grid-more-warn",
			"jv-grid-badge-warn",
			"jv-grid-ro",
			"is-empty",
			"missing",
			"invalid",
			"changed",
			"jv-draft-total-input",
		])
			expect([...used], c).toContain(c);
		expect(used.size).toBeGreaterThan(40);
	});

	it("is a scoped style block", () => {
		expect(formStyleTag.startsWith("<style scoped>")).toBe(true);
	});

	it("has a rule for every class in its template, or the class is allow-listed with a reason", () => {
		const styled = new Set(selectors(formCss).flatMap(classesIn));
		const bare = [...used].filter((c) => !styled.has(c) && !(c in NO_RULE_ALLOWED));
		expect(bare, "classes with no rule in DraftEditForm's style").toEqual([]);
		for (const c of Object.keys(NO_RULE_ALLOWED))
			expect(used.has(c), `${c} no longer used`).toBe(true);
	});

	it("depends on no panel-shell class: the phone sheet has no such ancestor", () => {
		const bad = selectors(formCss).filter((s) =>
			classesIn(s).some((c) => SHELL_CLASSES.includes(c))
		);
		expect(bad).toEqual([]);
		expect(stripComments(formCss)).not.toMatch(/\.jv-dark\b/);
	});

	it("keeps the root rule, the narrow-screen one-column rule and the shared copies", () => {
		const css = stripComments(formCss);
		expect(css).toMatch(/\.jv-draft-body\s*\{[^}]*overflow-y:\s*auto/);
		expect(css).toMatch(
			/@media \(max-width: 700px\)\s*\{\s*\.jv-draft-fields\s*\{\s*grid-template-columns:\s*1fr/
		);
		for (const sel of [
			".jv-sr",
			".jv-action-input",
			".jv-action-input:focus",
			".jv-action-sel",
		])
			expect(selectors(formCss), sel).toContain(sel);
		// `.jv-grid-opencell > :deep(.jv-rowform-block)` styles the root of DraftRowBlock
		expect(selectors(formCss)).toContain(".jv-grid-opencell > :deep(.jv-rowform-block)");
	});

	it("declares the shared input rules BEFORE the totals input that overrides them at equal specificity", () => {
		const sels = selectors(formCss);
		expect(sels.indexOf(".jv-action-input")).toBeGreaterThan(-1);
		expect(sels.indexOf(".jv-action-input")).toBeLessThan(
			sels.indexOf(".jv-draft-total-input")
		);
		expect(sels.indexOf(".jv-action-input:focus")).toBeLessThan(
			sels.indexOf(".jv-draft-total-input:focus")
		);
	});

	it("uses only the theme variables the host (and the phone's bridge) defines", () => {
		const vars = new Set([...formCss.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
		const known = new Set([
			"--text",
			"--text-2",
			"--text-3",
			"--surface",
			"--surface-1",
			"--surface-2",
			"--border",
			"--border-2",
			"--cta",
			"--cta-bg",
			"--cta-bd",
			// not in this file's rules; DraftRowBlock, DraftRowForm and DraftCell use it, so the phone
			// bridge must map it (see the B2a map: --cta-fg pairs with --cta, never alone)
			"--cta-fg",
			"--amber",
			"--amber-bg",
			"--amber-bd",
			"--red",
			"--red-bg",
			"--red-bd",
		]);
		expect([...vars].filter((v) => !known.has(v))).toEqual([]);
	});
});

describe("ChatView and the form share some classes: both must keep a rule", () => {
	// The draft summary card (which stays in ChatView) uses jv-rows-removed, jv-draft-parked, jv-req
	// and jv-sr too. Moving the form's rules out of ChatView must not strip them from the card.
	const sharedWithView = [...used].filter((c) => c.startsWith("jv-") && wordIn(viewNonStyle, c));

	it("finds the classes both templates use (so the check below is not vacuous)", () => {
		for (const c of ["jv-rows-removed", "jv-draft-parked", "jv-req", "jv-sr"])
			expect(sharedWithView, c).toContain(c);
	});

	it("ChatView's style still has a rule naming each of them", () => {
		const styled = new Set(selectors(viewCss).flatMap(classesIn));
		expect(sharedWithView.filter((c) => !styled.has(c) && !KEPT_IN_VIEW.has(c))).toEqual([]);
	});

	it("the form's style has a rule for each of them too", () => {
		const styled = new Set(selectors(formCss).flatMap(classesIn));
		expect(sharedWithView.filter((c) => !styled.has(c))).toEqual([]);
	});

	it("keeps the summary card's two base rules byte-for-byte in step with the form's copies", () => {
		const decl = (css, sel) => {
			const m = stripComments(css).match(
				new RegExp(`(?:^|\\})\\s*${sel.replace(".", "\\.")}\\s*\\{([^}]*)\\}`)
			);
			expect(m, `${sel} rule`).not.toBeNull();
			return m[1].replace(/\s+/g, " ").trim();
		};
		for (const sel of [".jv-rows-removed", ".jv-draft-parked"])
			expect(decl(viewCss, sel), sel).toBe(decl(formCss, sel));
	});
});

describe("ChatView keeps no rule for what moved", () => {
	// Classes that exist ONLY in the moved template: not in ChatView's own template. Only the jv-
	// ones: the bare state classes (missing, invalid, changed) are modifiers other cards reuse.
	const movedOnly = [...used].filter(
		(c) => c.startsWith("jv-") && !wordIn(viewNonStyle, c) && !KEPT_IN_VIEW.has(c)
	);

	it("has classes that are really moved-only (so the next check bites)", () => {
		for (const c of [
			"jv-draft-body",
			"jv-grid",
			"jv-grid-del",
			"jv-draft-totals",
			"jv-rows-changed",
		])
			expect(movedOnly, c).toContain(c);
		// still used by ChatView (jv-sr) or deliberately kept there (see KEPT_IN_VIEW)
		for (const c of [
			"jv-action-input",
			"jv-sr",
			"jv-rows-removed",
			"jv-draft-parked",
			"jv-req",
		])
			expect(movedOnly).not.toContain(c);
	});

	it("has no selector naming a class used only by the moved template", () => {
		const left = selectors(viewCss).filter((s) =>
			classesIn(s).some((c) => movedOnly.includes(c))
		);
		expect(left).toEqual([]);
	});

	it("still has the shell rules and the shared rules the moved form copied", () => {
		const sels = selectors(viewCss);
		for (const s of [
			".jv-draft-panel",
			".jv-draft-foot",
			".jv-draft-badge",
			".jv-action-input",
			".jv-sr",
		])
			expect(sels, s).toContain(s);
	});
});
