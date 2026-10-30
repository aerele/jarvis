import { test } from "node:test";
import assert from "node:assert/strict";
import { heroRoomPx } from "./holidayRoom.js";

test("heroRoomPx: room for the item's reach above the tile plus the gap", () => {
	assert.equal(heroRoomPx({ design: "christmas" }, false), 36 + 12);
	assert.equal(heroRoomPx({ design: "holi" }, false), 19 + 12);
});

test("heroRoomPx: nothing for a design that wears nothing above the tile", () => {
	assert.equal(heroRoomPx({ design: "diwali" }, false), 0);
});

test("heroRoomPx: nothing with no item to wear or in the upgrade hold", () => {
	assert.equal(heroRoomPx(null, false), 0);
	assert.equal(heroRoomPx({ design: "christmas" }, true), 0);
});
