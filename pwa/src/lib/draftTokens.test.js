import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { DRAFT_ALPHA, DRAFT_TOKENS, draftPalette } from "./draftTokens.js";

const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), "utf8");
const css = read("../index.css");
const theme = read("../../../frontend/src/theme.js");

// The declarations of one `{ ... }` block, as a name -> value object.
function declarations(block) {
	const out = {};
	for (const m of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim();
	return out;
}
const blockAt = (selector) => {
	const at = css.indexOf(selector + " {");
	assert.ok(at >= 0, `${selector} is in index.css`);
	return css.slice(at, css.indexOf("\n}", at));
};

// Break: the CSS the card and sheet read and the palette the teleported link menu reads drift
// apart (the menu would paint other colours than the form that opened it), or a name is dropped.
test("index.css .jv-draft-tokens equals the map the link menu's palette comes from", () => {
	assert.deepEqual(declarations(blockAt(".jv-draft-tokens")), DRAFT_TOKENS);
});

test("the palette is a fresh copy: a caller cannot change the map", () => {
	const p = draftPalette();
	assert.deepEqual(p, DRAFT_TOKENS);
	p["--surface"] = "red";
	assert.equal(DRAFT_TOKENS["--surface"], "var(--card)");
});

// Break: the names move to :root (or another global block). The export card and the document
// blocks read several of them with a fallback of their own; a global definition repaints them on
// the phone, and this commit must change no screen.
test("no phone token block defines the draft names globally", () => {
	for (const sel of [":root", ':root:not([data-theme="light"])', ':root[data-theme="dark"]']) {
		const defined = declarations(blockAt(sel));
		for (const name of Object.keys(DRAFT_TOKENS))
			assert.equal(name in defined, false, `${name} must not be defined in ${sel}`);
	}
});

// Break: the map points a name at a phone token that does not exist (the shared form would
// paint nothing for it), in light or in dark. Dark follows only because the dark blocks redefine
// the tokens the map points at.
test("every mapped value names phone tokens that exist in light and in dark", () => {
	const light = declarations(blockAt(":root"));
	const dark = declarations(blockAt(':root[data-theme="dark"]'));
	for (const [name, value] of Object.entries(DRAFT_TOKENS)) {
		const used = [...value.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]);
		assert.ok(used.length > 0, `${name} maps to a phone token`);
		for (const token of used) {
			assert.ok(token in light, `${name}: ${token} is a light phone token`);
			assert.ok(token in dark, `${name}: ${token} is redefined in dark`);
		}
	}
});

// Break: a shared component starts reading a colour name the phone does not define (it would
// paint nothing, with no fallback). Reads WITH a fallback are fine; names the component declares
// itself are fine. Today: every bare var(--x) is one of the 13, or a token the phone has.
test("every bare colour the shared draft components read is defined for the phone", () => {
	const files = [
		"chat/DraftEditForm.vue",
		"chat/DraftCell.vue",
		"chat/DraftLinkMenu.vue",
		"chat/DraftRowBlock.vue",
		"chat/DraftRowForm.vue",
		"doc/DraftPreview.vue",
	];
	const phone = new Set(Object.keys(declarations(blockAt(":root"))));
	for (const f of files) {
		const src = read("../../../frontend/src/components/" + f);
		const own = new Set([...src.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
		const bare = new Set([...src.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]));
		for (const name of bare)
			assert.ok(
				name in DRAFT_TOKENS || phone.has(name) || own.has(name),
				`${f} reads ${name}, which the phone does not define`
			);
	}
});

// Break: the two border tints drift from the desktop's (a warning border that reads as a
// different colour on the phone). The desktop's tints are opaque; each equals its amber / red at
// some alpha over its surface. Fit that alpha per theme and require ours within 3 points.
test("the amber and red border tints use the alpha the desktop's tints work out to", () => {
	const hexes = (name) =>
		[...theme.matchAll(new RegExp(`"${name}":\\s*"(#[0-9a-f]{6})"`, "g"))].map((m) => m[1]);
	const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
	const fit = (color, tint, surface) => {
		const [c, t, s] = [color, tint, surface].map(rgb);
		let num = 0;
		let den = 0;
		for (let i = 0; i < 3; i++) {
			num += (c[i] - s[i]) * (t[i] - s[i]);
			den += (c[i] - s[i]) ** 2;
		}
		return (num / den) * 100;
	};
	const [surfaceL, surfaceD] = hexes("--surface");
	assert.ok(surfaceL && surfaceD);
	for (const colour of ["amber", "red"]) {
		const [lightColour, darkColour] = hexes(`--${colour}`);
		const [lightTint, darkTint] = hexes(`--${colour}-bd`);
		for (const alpha of [
			fit(lightColour, lightTint, surfaceL),
			fit(darkColour, darkTint, surfaceD),
		])
			assert.ok(
				Math.abs(alpha - DRAFT_ALPHA) <= 3,
				`${colour}: desktop alpha ${alpha.toFixed(1)} vs ${DRAFT_ALPHA}`
			);
		assert.match(
			DRAFT_TOKENS[`--${colour}-bd`],
			new RegExp(`var\\(--${colour}\\) ${DRAFT_ALPHA}%`)
		);
	}
});
