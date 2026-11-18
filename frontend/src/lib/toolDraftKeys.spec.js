// The keyboard of the panels of a server draft card (lib/toolDraftKeys.js): what Escape does,
// where Tab goes, where focus lands, and how the opener button is found again.
import { describe, expect, it } from "vitest";
import {
	firstEditable,
	openerButton,
	tabWrapTarget,
	tabbables,
	toolPanelKeyAction,
} from "./toolDraftKeys.js";

const tree = (html) => {
	const root = document.createElement("div");
	root.innerHTML = html;
	document.body.appendChild(root);
	return root;
};

describe("toolPanelKeyAction: what Escape does", () => {
	// Break: Escape closes a form that holds edits (they are lost), does nothing in the Preview, or
	// takes a key the link menu / row form / a dialog / an IME already took.
	const table = [
		["the Preview closes", { preview: true }, "close-preview"],
		["a clean form closes", { form: true }, "close-form"],
		[
			"a form with edits stays and takes focus back",
			{ form: true, dirty: true },
			"refocus-form",
		],
		[
			"the Preview wins when both are somehow open",
			{ preview: true, form: true },
			"close-preview",
		],
		[
			"a link menu is open: only the menu closes",
			{ form: true, linkMenu: true },
			"close-menu",
		],
		[
			"a link menu is open over edits: still only the menu",
			{ form: true, dirty: true, linkMenu: true },
			"close-menu",
		],
		["already handled inside the panel", { form: true, defaultPrevented: true }, null],
		["an IME composition", { form: true, composing: true }, null],
		["a dialog is open", { preview: true, dialogOpen: true }, null],
		["no panel", {}, null],
		["another key", { form: true, key: "Enter" }, null],
	];
	for (const [name, over, want] of table)
		it(name, () => expect(toolPanelKeyAction({ key: "Escape", ...over })).toBe(want));
});

describe("tabWrapTarget: Tab stays inside the open panel", () => {
	const root = tree(`<div id="panel"><button id="a">a</button><input id="b">
		<button id="off" disabled>x</button><button id="skip" tabindex="-1">y</button>
		<a id="c" href="#">c</a></div><button id="outside">o</button>`);
	const panel = root.querySelector("#panel");
	const id = (el) => (el ? el.id : null);
	const at = (sel) => root.querySelector(sel);
	// Break: Tab leaves the panel from its last control, Shift+Tab from its first, focus outside the
	// panel is not brought in, or a disabled or skipped control is counted.
	const table = [
		["last control, Tab: wraps to the first", "#c", false, "a"],
		["first control, Shift+Tab: wraps to the last", "#a", true, "c"],
		["a middle control: the browser moves focus", "#b", false, null],
		["a middle control, Shift+Tab: the browser moves focus", "#b", true, null],
		["focus outside the panel comes in at the first", "#outside", false, "a"],
		["focus outside, Shift+Tab, comes in at the last", "#outside", true, "c"],
		["the panel itself: first", "#panel", false, "a"],
	];
	for (const [name, from, shift, want] of table)
		it(name, () => expect(id(tabWrapTarget(panel, at(from), shift))).toBe(want));
	it("no active element comes in at the first control; no controls gives nothing", () => {
		expect(id(tabWrapTarget(panel, null, false))).toBe("a");
		expect(tabWrapTarget(tree("<div></div>"), null, false)).toBeNull();
		expect(tabbables(panel).map(id)).toEqual(["a", "b", "c"]);
	});
});

describe("firstEditable: where focus lands when the form opens", () => {
	// Break: focus lands on a read-only value, a hidden input, or a Link input (which opens its
	// suggestions menu and a search before the person asked for it), or on the close button
	// when the form has a field.
	const form = (body) =>
		tree(
			`<aside><button id="x">close</button><div class="jv-draft-body">${body}</div></aside>`
		);
	const link = `<input id="link" autocomplete="off" placeholder="Search Employee…">`;
	it("the first editable field", () => {
		expect(
			firstEditable(
				form(`<input id="ro" readonly><input type="hidden"><select id="s"></select>`)
			).id
		).toBe("s");
	});
	it("skips a Link input", () => {
		expect(firstEditable(form(`${link}<input id="text">`)).id).toBe("text");
	});
	it("with no editable field: the first control of the panel, else the panel", () => {
		expect(firstEditable(form(link)).id).toBe("x");
		const bare = tree(`<aside id="bare"></aside>`).firstChild;
		expect(firstEditable(bare)).toBe(bare);
		expect(firstEditable(null)).toBeNull();
	});
});

describe("openerButton: the card button that opened a panel, found again", () => {
	const card = (token, preview = "") => `<div data-draft-token="${token}">
		<button class="dc-btn"><span class="dc-label">Create</span></button>
		<button class="dc-btn"${preview}><span class="dc-label">Preview</span></button>
		<button class="dc-btn"><span class="dc-label">Edit</span></button></div>`;
	// Break: focus returns to another card's button, a disabled button (it cannot take focus) or
	// the wrong button; or throws when the card is gone.
	it("finds the button by token and label", () => {
		const root = tree(card("t1") + card("t2"));
		expect(openerButton(root, "t2", "Edit").textContent.trim()).toBe("Edit");
		expect(
			openerButton(root, "t2", "Edit").closest("[data-draft-token]").dataset.draftToken
		).toBe("t2");
	});
	it("is null for a gone card, an unknown label or a disabled button", () => {
		const root = tree(card("t1", " disabled"));
		expect([
			openerButton(root, "gone", "Edit"),
			openerButton(root, "t1", "Nope"),
			openerButton(root, "t1", "Preview"),
			openerButton(null, "t1", "Edit"),
		]).toEqual([null, null, null, null]);
	});
});
