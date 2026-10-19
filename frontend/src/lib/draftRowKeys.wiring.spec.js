// Source-level checks that ChatView is wired to the row keys and the row form's focus
// helpers: the view is too big to mount. The behaviour itself is spec'd with
// real components and key events in DraftRowBlock.spec.js, rowFormFocus in
// draftTables/draftLinkFilters specs.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const between = (s, from, to) => {
	const i = s.indexOf(from);
	expect(i).toBeGreaterThan(-1);
	return s.slice(i, s.indexOf(to, i));
};
const grid = between(src, '<table class="jv-grid">', "</table>");
const forms = between(src, "<DraftRowBlock", "/>");

describe("ChatView keys draft rows by __key", () => {
	it("the grid rows, the form blocks and the link-search keys use __key, not the index", () => {
		expect(grid).toMatch(/v-for="\(r, ri\) in t\.rows" :key="r\.__key"/);
		expect(src).toMatch(/v-for="\(r, ri\) in t\.rows" :key="'form-' \+ r\.__key"/);
		expect(grid).toMatch(/':' \+\s+r\.__key \+\s+':' \+\s+c\.fieldname/);
		expect(src).not.toMatch(/v-for="\(r, ri\) in t\.rows" :key="ri"/);
	});
	it("what stays index-based: Remove, the 'Row N' labels, the row status", () => {
		expect(grid).toMatch(/removeDraftRow\(ti, ri\)/);
		expect(grid).toMatch(/'Remove row ' \+ \(ri \+ 1\)/);
		expect(forms).toMatch(/:row-index="ri"/);
		expect(grid).toMatch(/rowStatusFor\(\s*t,\s*r,\s*ri\s*\)/);
	});
});

describe("ChatView focus for the row form", () => {
	it("only the toggle click moves focus in, through toggleRowForm", () => {
		const toggle = between(grid, ':id="rowToggleId', "</button>");
		expect(toggle).toMatch(/@click="toggleRowForm\(ti, r\)"/);
		expect(toggle).toMatch(/:id="rowToggleId\(ti, r\.__key\)"/);
		expect(toggle).toMatch(/rowFormId\(ti, r\.__key\)/);
		expect(src).not.toMatch(/r\.__open = !r\.__open/);
	});
	it("Done and Escape close through closeRowForm, which returns focus by key", () => {
		expect(forms).toMatch(/@close="closeRowForm\(ti, r\)"/);
		expect(src).not.toMatch(/@click="r\.__open = false"/);
	});
	it("Escape is local to the block, not the window handler", () => {
		const fn = between(src, "function onGlobalKey(e) {", "// ---- Publish chat context");
		expect(fn).not.toMatch(/__open|closeRowForm/);
		const block = fs.readFileSync(
			path.resolve(__dirname, "../components/chat/DraftRowBlock.vue"),
			"utf8"
		);
		expect(block).toMatch(/@keydown\.esc="onEscape"/);
		expect(block).toMatch(/tabindex="-1"/);
	});
});
