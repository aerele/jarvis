import { test } from "node:test";
import assert from "node:assert/strict";
import { isWhitelabelBrand } from "./brand.js";

test("isWhitelabelBrand: a logo or a non-default agent name, nothing else", () => {
	assert.equal(isWhitelabelBrand("Jarvis", ""), false);
	assert.equal(isWhitelabelBrand("Jarvis", "/files/logo.png"), true);
	assert.equal(isWhitelabelBrand("Acme Assistant", ""), true);
});
