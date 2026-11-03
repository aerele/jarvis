import { test } from "node:test";
import assert from "node:assert/strict";
import { heroWords } from "./holidayWords.js";

const day = (occasion) => ({ v: 1, kind: "day", occasion, design: occasion });
const base = { greeting: "Good morning, Priya.", firstName: "Priya" };

test("heroWords: no answer keeps the ordinary greeting", () => {
	assert.deepEqual(heroWords({ v: 1, kind: "none" }, base), {
		heading: "Good morning, Priya.",
		leadup: null,
	});
});

test("heroWords: a lead-up keeps the greeting and adds the advance wish under it", () => {
	const answer = { v: 1, kind: "leadup", occasion: "diwali", design: "diwali" };
	assert.deepEqual(heroWords(answer, base), {
		heading: "Good morning, Priya.",
		leadup: "Wishing you a happy Diwali in advance!",
	});
});

test("heroWords: the day heading names the person and keeps the greeting's full stop", () => {
	assert.equal(heroWords(day("diwali"), base).heading, "Happy Diwali, Priya.");
	assert.equal(heroWords(day("diwali"), base).leadup, null);
});

test("heroWords: a birthday has its own heading and no advance wish", () => {
	const answer = { v: 1, kind: "birthday", occasion: "birthday", design: "birthday" };
	assert.deepEqual(heroWords(answer, base), { heading: "Happy birthday, Priya.", leadup: null });
});

test("heroWords: no name means no dangling comma", () => {
	const words = heroWords(day("christmas"), { greeting: "Good evening.", firstName: "" });
	assert.equal(words.heading, "Merry Christmas.");
});

test("heroWords: an unknown occasion falls back to the greeting", () => {
	assert.deepEqual(heroWords(day("toString"), base), {
		heading: "Good morning, Priya.",
		leadup: null,
	});
});
