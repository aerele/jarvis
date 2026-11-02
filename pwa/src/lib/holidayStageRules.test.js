import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveTheme, sizeChanged, stageEligible } from "./holidayStageRules.js";

const day = { kind: "day", scene: true, worn: true, design: "diwali" };
const open = { whiteLabel: false, holdAtMount: false, hasScene: () => true };

test("stageEligible: a scene day with an item to wear and a scene in this build", () => {
	assert.equal(stageEligible({ answer: day, ...open }), true);
	assert.equal(stageEligible({ answer: { ...day, kind: "birthday" }, ...open }), true);
});

test("stageEligible: everything else keeps the ordinary welcome", () => {
	assert.equal(stageEligible({ answer: null, ...open }), false);
	assert.equal(stageEligible({ answer: { ...day, kind: "leadup" }, ...open }), false);
	assert.equal(stageEligible({ answer: { ...day, scene: false }, ...open }), false);
	assert.equal(stageEligible({ answer: { ...day, worn: false }, ...open }), false);
	assert.equal(stageEligible({ answer: day, ...open, whiteLabel: true }), false);
	assert.equal(stageEligible({ answer: day, ...open, holdAtMount: true }), false);
	assert.equal(stageEligible({ answer: day, ...open, bannerAtMount: true }), false);
	assert.equal(stageEligible({ answer: day, ...open, hasScene: () => false }), false);
});

test("resolveTheme: a pinned theme wins, system follows the OS", () => {
	assert.equal(resolveTheme("dark", false), "dark");
	assert.equal(resolveTheme("light", true), "light");
	assert.equal(resolveTheme("system", true), "dark");
	assert.equal(resolveTheme("system", false), "light");
	assert.equal(resolveTheme(undefined, true), "dark");
});

test("sizeChanged: only a move of 1 px or more counts, and nothing known yet is no change", () => {
	const a = { w: 390, h: 844 };
	assert.equal(sizeChanged(a, { w: 390, h: 844 }), false);
	assert.equal(sizeChanged(a, { w: 390.4, h: 843.6 }), false);
	assert.equal(sizeChanged(a, { w: 390, h: 843 }), true);
	assert.equal(sizeChanged(a, { w: 391, h: 844 }), true);
	assert.equal(sizeChanged(undefined, a), false);
});
