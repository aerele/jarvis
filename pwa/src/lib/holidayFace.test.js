import { test } from "node:test";
import assert from "node:assert/strict";
import { MOODS, effectiveMood, faceShown, stageMood } from "./holidayFace.js";

test("MOODS: the moods the mark can show", () => {
	assert.deepEqual(MOODS, ["star", "happy", "upgrading", "peek"]);
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

test("MOODS: a peek is a face with no smile, driven by the scene", () => {
	assert.ok(MOODS.includes("peek"));
	assert.equal(faceShown("peek"), true);
});

test("stageMood: the scene's beats choose the face, never over the upgrade hold", () => {
	assert.equal(stageMood("star", { face: false, happy: false }), "star");
	assert.equal(stageMood("star", { face: true, happy: false }), "peek");
	assert.equal(stageMood("star", { face: true, happy: true }), "happy");
	assert.equal(stageMood("upgrading", { face: true, happy: true }), "upgrading");
});
