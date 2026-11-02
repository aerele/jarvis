import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PAGE } from "../../../jarvis/public/js/shared/holiday/scenes/phone-boxes.test-support.mjs";
import { heroBox, sceneBoxes } from "./holidayBoxes.js";

const rects = {
	host: { w: 390, h: 726 },
	hero: { x: 65, y: 220, w: 260, h: 120 },
	mark: { x: 167, y: 220, w: 56, h: 56 },
	heading: { x: 80, y: 290, w: 230, h: 30 },
};

test("sceneBoxes: the starter band is the cards box when the cards are on screen", () => {
	const cards = { x: 14, y: 500, w: 362, h: 200 };
	const boxes = sceneBoxes({ ...rects, cards });
	assert.deepEqual(boxes.cards, cards);
	assert.deepEqual(boxes.host, rects.host);
	assert.deepEqual(boxes.mark, rects.mark);
});

test("sceneBoxes: with no cards (a short screen) the band is the room under the hero", () => {
	for (const cards of [null, { x: 0, y: 0, w: 0, h: 0 }]) {
		const boxes = sceneBoxes({ ...rects, cards });
		assert.equal(boxes.cards.y, 220 + 120 + 16);
		assert.equal(boxes.cards.h, 726 - 8 - (220 + 120 + 16));
		assert.equal(boxes.cards.x, 14);
		assert.equal(boxes.cards.w, 390 - 28);
	}
});

test("sceneBoxes: a hero that leaves no room gives an empty band, never a negative one", () => {
	const boxes = sceneBoxes({ ...rects, hero: { ...rects.hero, y: 700 }, cards: null });
	assert.equal(boxes.cards.h, 0);
});

test("heroBox: the mark and the heading together, not the flex area around them", () => {
	const mark = { x: 167, y: 220, w: 56, h: 56 };
	const heading = { x: 80, y: 290, w: 230, h: 52 };
	assert.deepEqual(heroBox(mark, heading), { x: 80, y: 220, w: 230, h: 122 });
});

// The test layouts of the scene bounds tests (phoneBoxes) stand for this page. The sizes its CSS
// sets are checked here against the view, so a restyle cannot leave the tests drawing a page
// that no longer exists. (The bar, composer, hero and starter band depend on content: they are
// measured on the real page and noted in phone-boxes.test-support.mjs.)
const view = readFileSync(new URL("../views/NewChatView.vue", import.meta.url), "utf8");
const rule = (selector) => view.match(new RegExp(`\\n${selector} \\{([^}]*)\\}`))?.[1] ?? "";

test("the scene bounds tests draw the page's mark, hero gap and greeting size", () => {
	assert.match(view, new RegExp(`data-hs="mark"\\s+:size="${PAGE.MARK_PX}"`));
	assert.match(rule("\\.jv-hero"), new RegExp(`gap: ${PAGE.HERO_GAP_PX}px`));
	assert.match(rule("\\.jv-greeting"), new RegExp(`font-size: ${PAGE.GREETING_PX}px`));
});
