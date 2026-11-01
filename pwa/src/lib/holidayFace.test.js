import { test } from "node:test";
import assert from "node:assert/strict";
import { MOODS, effectiveMood, faceShown } from "./holidayFace.js";

test("MOODS: the moods the mark can show", () => {
	assert.deepEqual(MOODS, ["star", "happy", "upgrading"]);
});

test("faceShown: any mood other than the resting star shows the face", () => {
	assert.equal(faceShown("star"), false);
	assert.equal(faceShown("happy"), true);
	assert.equal(faceShown("upgrading"), true);
});

test("effectiveMood: a smile only replaces the resting star", () => {
	assert.equal(effectiveMood("star", true), "happy");
	assert.equal(effectiveMood("star", false), "star");
	// The upgrade hold owns the face: its sleepy lids are telling the user something.
	assert.equal(effectiveMood("upgrading", true), "upgrading");
});
