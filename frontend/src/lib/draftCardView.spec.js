import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { draftCardView } from "./draftCardView.js";
import { draftCardCases } from "./draftCardViewCases.js";

// The checks live in draftCardViewCases.js, shared with pwa/src/lib/draftCardView.test.js.
const FIXTURES = JSON.parse(
	fs.readFileSync(path.resolve(__dirname, "draftCardFixtures.json"), "utf8")
);

describe("draftCardView shared cases", () => {
	const cases = draftCardCases(draftCardView, FIXTURES);
	for (const c of cases) it(c.name, () => expect(c.actual).toEqual(c.expected));
});
