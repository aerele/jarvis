// Source-level checks that ChatView is wired to the changed-rows helpers (the view is
// too big to mount): the logic itself is spec'd in draftTables.spec.js / rowForm.spec.js.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const tables = fs.readFileSync(path.resolve(__dirname, "./draftTables.js"), "utf8");
const between = (s, from, to) => {
	const i = s.indexOf(from);
	expect(i).toBeGreaterThan(-1);
	return s.slice(i, s.indexOf(to, i));
};

describe("ChatView shows what changed outside the grid", () => {
	it("auto-opens on a build of the panel, passing the panel that was open", () => {
		const fn = between(src, "async function openDraftPanel(a) {", "function addDraftRow");
		expect(fn).toMatch(/applyAutoOpen\(model, draftPanel\.value\)/);
		// before the model is stamped into the panel, so the first render already has the rows open
		expect(fn.indexOf("applyAutoOpen(")).toBeLessThan(
			fn.indexOf("draftPanel.value = stampDraftModel")
		);
	});
	it("never moves focus when it opens rows", () => {
		const fn = between(src, "async function openDraftPanel(a) {", "function addDraftRow");
		expect(fn).not.toMatch(/\.focus\(|nextTick/);
		expect(between(tables, "export function applyAutoOpen", "// A row toggle's")).not.toMatch(
			/focus/
		);
		expect(
			between(tables, "export function openChangedRows", "// On the first build")
		).not.toMatch(/focus/);
	});
	it("renders the summary above the grid, with the button only when grouped", () => {
		const t = between(src, 'class="jv-rows-changed"', 'class="jv-draft-gridwrap"');
		expect(t).toMatch(/draftChangeSummaries\[ti\]\.text/);
		expect(t).toMatch(/v-if="draftChangeSummaries\[ti\]\.grouped"/);
		expect(t).toMatch(/openChangedRows\(t, draftChangeSummaries\[ti\]\)/);
	});
	it("labels the grouped button with the count, not a fixed phrase", () => {
		const t = between(src, 'class="jv-rows-changed"', 'class="jv-draft-gridwrap"');
		expect(t).toMatch(/draftChangeSummaries\[ti\]\.openLabel/);
		expect(t).not.toMatch(/Open changed rows/);
	});
	it("renders each named row number as a button that opens that row and focuses its form", () => {
		const t = between(src, 'class="jv-rows-changed"', 'class="jv-draft-gridwrap"');
		expect(t).toMatch(/v-else/);
		expect(t).toMatch(/v-for="i in draftChangeSummaries\[ti\]\.rows"/);
		expect(t).toMatch(/@click="showRowForm\(ti, t\.rows\[i\]\)"/);
		expect(t).toMatch(/\{\{ i \+ 1 \}\}/);
		expect(src).toMatch(/import \{[^}]*showRowForm[^}]*\} from "@\/lib\/rowFormFocus"/);
	});
	it("a throw in the auto-open can never stop the panel opening", () => {
		const fn = between(src, "async function openDraftPanel(a) {", "function addDraftRow");
		expect(fn).toMatch(
			/try \{\s*applyAutoOpen\(model, draftPanel\.value\);\s*\} catch \(e\) \{/
		);
		expect(fn).toMatch(/console\.warn\(/);
		// and the panel is still set after it
		expect(fn.indexOf("catch (e) {", fn.indexOf("applyAutoOpen("))).toBeLessThan(
			fn.indexOf("draftPanel.value = stampDraftModel")
		);
	});
	it("the toggle shows short text from toggleParts, and its title is its aria-label", () => {
		const t = between(src, 'class="jv-grid-more"', "</button>");
		expect(t).toMatch(/toggleParts\(/);
		expect(t).toMatch(/:title="rowToggleLabel\(t, r, ri\)"/);
		expect(t).toMatch(/:aria-label="rowToggleLabel\(t, r, ri\)"/);
		expect(t).not.toMatch(/v-else-if/); // missing and changed both show
	});
	it("the row form block gets the saved row, for 'was X' and the rule-hidden rule", () => {
		expect(between(src, "<DraftRowBlock", "/>")).toMatch(
			/:saved-row="rowStatusFor\(t, r, ri\)\.savedRow"/
		);
	});
});
