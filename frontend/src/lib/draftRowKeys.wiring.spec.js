// Source-level checks that the draft panel is wired to the row keys and the row form's focus
// helpers: ChatView is too big to mount, and the form's template lives in DraftEditForm.vue. The behaviour itself is spec'd with
// real components and key events in DraftRowBlock.spec.js, rowFormFocus in
// draftTables/draftLinkFilters specs.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const link = fs.readFileSync(path.resolve(__dirname, "./useDraftLink.js"), "utf8");
const form = fs.readFileSync(
	path.resolve(__dirname, "../components/chat/DraftEditForm.vue"),
	"utf8"
);
// Both markers must exist: a missing end marker must fail, never slice to the end of the file.
const between = (s, from, to) => {
	const i = s.indexOf(from);
	expect(i, `start marker not found: ${from}`).toBeGreaterThan(-1);
	const j = s.indexOf(to, i);
	expect(j, `end marker not found: ${to}`).toBeGreaterThan(-1);
	return s.slice(i, j);
};
const grid = between(form, '<table class="jv-grid">', "</table>");
const forms = between(form, "<DraftRowBlock", "/>");
// The two sources the old single-file checks covered: nothing of the old shapes may return to either.
const both = src + "\n" + form;

describe("ChatView keys draft rows by __key", () => {
	it("the grid rows, the form blocks and the link-search keys use __key, not the index", () => {
		expect(grid).toMatch(/v-for="\(r, ri\) in t\.rows" :key="r\.__key"/);
		// An open row's form is its grid row (in place), so it shares that row's key.
		expect(grid).toContain("<DraftRowBlock");
		expect(form.split("<DraftRowBlock").length - 1).toBe(1);
		expect(src).not.toContain("<DraftRowBlock"); // the form owns it; the view mounts the form
		expect(grid).toMatch(/':' \+\s+r\.__key \+\s+':' \+\s+c\.fieldname/);
		expect(both).not.toMatch(/v-for="\(r, ri\) in t\.rows" :key="ri"/);
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
		expect(both).not.toMatch(/r\.__open = !r\.__open/);
	});
	it("Done and Escape close through closeRowForm, which returns focus by key", () => {
		expect(forms).toMatch(/@close="closeRowForm\(ti, r\)"/);
		expect(both).not.toMatch(/@click="r\.__open = false"/);
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

describe("ChatView fetches the rule values Desk fetches from a link", () => {
	// The build's own wait and the late-answer rule are executed in draftModel.spec.js and
	// draftModel.cards.spec.js; here only the view's wiring of the shared builder is pinned.
	it("the builder is made with this view's calls, before the link menu is handed its fillRuleValues", () => {
		const made = between(src, "makeDraftModelBuilder({", "});");
		// Break: a call wired to the wrong api function (a saved-record read answering the form
		// meta, or the rule values never asked).
		expect(made).toContain("api.getDoctypeFormMeta(doctype)");
		expect(made).toContain("api.loadDocForEdit(doctype, name)");
		expect(made).toContain("api.ruleValues(doctype, table, links)");
		// Break: the view stops taking the builder's form-meta cache (a failed save then marks no
		// field the form does not show: markMissing, the Save error path and the tool host's
		// metaFields all read `_formMetaCache[doctype]?.fields` and tolerate its absence silently).
		expect(between(src, "const {\n\tbuildDraftModel,", "makeDraftModelBuilder({")).toContain(
			"formMetaCache: _formMetaCache,\n"
		);
		// Break: the builder moved below useDraftLink. fillRuleValues is a const from it now, and
		// useDraftLink reads it while the view is set up: the view would not mount.
		expect(src.indexOf("makeDraftModelBuilder({")).toBeLessThan(src.indexOf("useDraftLink({"));
		// The pick handler lives in the shared composable; the host hands it its fillRuleValues.
		expect(between(link, "function pickDraftLink(", "\n\tfunction ")).toMatch(
			/fillRuleValues\(panel\.doctype, panel\.tables, \(\) => draftPanel\.value === panel\)/
		);
		expect(between(src, "useDraftLink({", "});")).toMatch(/draftPanel,[\s\S]*fillRuleValues,/);
	});
});

describe("ChatView clears the link search where the draft panel goes away", () => {
	it("closing the panel and the conversation-switch reset both call resetDraftLink", () => {
		// Catches: either call removed, so an in-flight search reopens its menu over the next draft.
		expect(between(src, "function closeDraftPanel() {", "\n}\n")).toContain(
			"resetDraftLink()"
		);
		expect(between(src, "function resetDraftActionState() {", "\n}\n")).toContain(
			"resetDraftLink()"
		);
	});
});
