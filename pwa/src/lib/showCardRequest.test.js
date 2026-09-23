import { test } from "node:test";
import assert from "node:assert/strict";
import { isShowCardRequest } from "./showCardRequest.js";

const POSITIVES = [
	"show it",
	"show the card",
	"show the confirmation",
	"i can't see it",
	"i can't see the card",
	"i can't see the confirmation",
	"the confirmation didn't appear",
	"the card didn't appear",
];

const NEGATIVES = [
	"show me the sales orders",
	"can you show it",
	"show it to me later",
	"don't show it",
	"do not show it",
	"don’t show it", // negation with a curly apostrophe
	"i can't see the chart",
	// one substring-superset per phrase: a substring/prefix regression would swallow these
	"the confirmation didn't appear in the report",
	"please show the confirmation now",
	"i can't see it anymore",
	"i can't see the card in the list",
	"i can't see the confirmation dialog",
	"the card didn't appear yet",
	"please show the card now",
	"",
	"   ",
];

test("isShowCardRequest matches each exact phrase", () => {
	for (const p of POSITIVES) assert.equal(isShowCardRequest(p), true, p);
});

test("isShowCardRequest normalises case / whitespace / trailing punctuation / curly apostrophe", () => {
	assert.equal(isShowCardRequest("Show It"), true);
	assert.equal(isShowCardRequest("  show it  "), true);
	assert.equal(isShowCardRequest("show it."), true);
	assert.equal(isShowCardRequest("show   it"), true);
	assert.equal(isShowCardRequest("show it"), true); // non-breaking space
	assert.equal(isShowCardRequest("show it ."), true); // space before trailing punctuation
	assert.equal(isShowCardRequest("I can’t see the card"), true);
});

test("isShowCardRequest does not fire on prefixes / negations / extra words / prose", () => {
	for (const n of NEGATIVES) assert.equal(isShowCardRequest(n), false, n);
	assert.equal(isShowCardRequest("Please show it to finance by Friday."), false);
});

test("isShowCardRequest caps length and is non-string safe", () => {
	assert.equal(isShowCardRequest("show it " + "x".repeat(60)), false);
	assert.equal(isShowCardRequest(null), false);
	assert.equal(isShowCardRequest(42), false);
	assert.equal(isShowCardRequest(["show it"]), false);
});
