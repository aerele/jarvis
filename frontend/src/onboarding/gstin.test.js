// GSTIN validation: shape + state code + mod-36 checksum. Pure, node --test.

import { test } from "node:test";
import assert from "node:assert/strict";

import { isValidGstin, gstinError, GSTIN_PLACEHOLDER } from "./gstin.js";

test("GSTIN is optional: a blank value is never an error", () => {
	assert.equal(gstinError(""), "");
	assert.equal(gstinError("   "), "");
	assert.equal(gstinError(undefined), "");
	assert.equal(gstinError(null), "");
	// isValidGstin is the strict "is this literally a GSTIN" check, so blank is
	// not a valid GSTIN even though gstinError treats it as no error.
	assert.equal(isValidGstin(""), false);
});

test("GSTIN_PLACEHOLDER is checksum-valid and case/whitespace-tolerant", () => {
	assert.equal(isValidGstin(GSTIN_PLACEHOLDER), true);
	assert.equal(gstinError(GSTIN_PLACEHOLDER), "");
	// isValidGstin trims + uppercases before checking, per spec.
	assert.equal(isValidGstin(`  ${GSTIN_PLACEHOLDER.toLowerCase()}  `), true);
});

test("a 23-character junk string fails on length, not shape or checksum", () => {
	const junk = "12345678901234567890123";
	assert.equal(junk.length, 23);
	assert.equal(isValidGstin(junk), false);
	assert.equal(gstinError(junk), "GSTIN must be 15 characters.");
});

test("a 15-character string with the wrong shape is rejected", () => {
	// Right length, but digits where the PAN letters/checksum letter must be.
	const wrongShape = "123456789012345";
	assert.equal(wrongShape.length, 15);
	assert.equal(isValidGstin(wrongShape), false);
	assert.equal(
		gstinError(wrongShape),
		"That doesn't look like a GSTIN, check the letters and numbers."
	);
});

test("an unrecognized state code is rejected even with a well-shaped GSTIN", () => {
	// "00" is not an issued state/UT code (valid range is 01-38, plus 97 and 99).
	const badState = "00ABCDE1234F1Z5";
	assert.equal(isValidGstin(badState), false);
	assert.equal(gstinError(badState), "GSTIN has an unrecognized state code.");
});

// The admin control plane rejects this exact value with "billing.gstin failed
// its checksum check" - the current onboarding placeholder used to be this
// string, so a customer copying the placeholder verbatim always failed at
// checkout. The correct check char for "33ABCDE1234F1Z" is "7", not "5".
test("33ABCDE1234F1Z5 fails the checksum specifically", () => {
	const badChecksum = "33ABCDE1234F1Z5";
	assert.equal(isValidGstin(badChecksum), false);
	assert.equal(gstinError(badChecksum), "GSTIN doesn't check out, please look for a typo.");
	// Swapping in the correct check char makes it pass, and matches the placeholder.
	assert.equal(isValidGstin("33ABCDE1234F1Z7"), true);
	assert.equal(GSTIN_PLACEHOLDER, "33ABCDE1234F1Z7");
});
