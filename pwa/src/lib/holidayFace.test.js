import { test } from "node:test";
import assert from "node:assert/strict";
import { MOODS, effectiveMood, faceShown, isWhitelabelBrand, layerState } from "./holidayFace.js";

test("MOODS: the moods the mark can show", () => {
	assert.deepEqual(MOODS, ["star", "happy", "upgrading"]);
});

test("faceShown: a mood other than star, or a peek, shows the face", () => {
	assert.equal(faceShown("star", false), false);
	assert.equal(faceShown("happy", false), true);
	assert.equal(faceShown("upgrading", false), true);
	assert.equal(faceShown("star", true), true);
});

test("effectiveMood: a smile only replaces the resting star", () => {
	assert.equal(effectiveMood("star", true), "happy");
	assert.equal(effectiveMood("star", false), "star");
	// The upgrade hold owns the face: its sleepy lids are telling the user something.
	assert.equal(effectiveMood("upgrading", true), "upgrading");
});

test("layerState: how the worn item reacts to the mood", () => {
	assert.equal(layerState("happy"), "happy");
	assert.equal(layerState("star"), "peek");
	assert.equal(layerState("upgrading"), "peek");
});

test("isWhitelabelBrand: a logo or a non-default agent name, nothing else", () => {
	assert.equal(isWhitelabelBrand("Jarvis", ""), false);
	assert.equal(isWhitelabelBrand("Jarvis", "/files/logo.png"), true);
	assert.equal(isWhitelabelBrand("Acme Assistant", ""), true);
});
