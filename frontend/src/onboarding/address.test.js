// Address consistency (State <-> GSTIN <-> Pincode). Pure, node --test. Mirrors the control-plane matrix.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
	addressConsistencyError,
	pincodeMatchesState,
	stateCodeFromGstin,
	stateCodeFromName,
	STATE_BY_CODE,
	STATE_PINCODE_MAPPING,
} from "./address.js";

const err = (o) => addressConsistencyError(o);

test("consistent India address passes", () => {
	assert.equal(err({ country: "India", state: "Maharashtra", pincode: "400001" }), "");
	assert.equal(
		err({
			country: "India",
			state: "Tamil Nadu",
			pincode: "600001",
			gstin: "33ABCDE1234F1Z7",
		}),
		""
	);
});

test("pincode out of range for the state is flagged (the trigger case)", () => {
	const m = err({ country: "India", state: "Maharashtra", pincode: "638108" }); // 638 is Tamil Nadu
	assert.match(m, /postal code/i);
	assert.equal(err({ country: "India", state: "Tamil Nadu", pincode: "638108" }), "");
});

test("GSTIN state code must match the state", () => {
	assert.match(
		err({ country: "India", state: "Karnataka", gstin: "33ABCDE1234F1Z7" }),
		/GSTIN/i
	); // 33 vs KA
	assert.equal(err({ country: "India", state: "Tamil Nadu", gstin: "33ABCDE1234F1Z7" }), "");
});

test("GSTIN with no known canonical state (99 Centre Jurisdiction) is not false-flagged", () => {
	assert.equal(stateCodeFromGstin("99AAAAA0000A1Z5"), null);
	assert.equal(err({ country: "India", state: "Maharashtra", gstin: "99AAAAA0000A1Z5" }), "");
});

test("malformed pincode is flagged (6-digit, no leading zero)", () => {
	for (const bad of ["1234", "12345", "0400001", "038108"]) {
		assert.match(
			err({ country: "India", state: "Maharashtra", pincode: bad }),
			/6-digit/i,
			bad
		);
	}
});

test("not-checkable inputs pass: blank pincode, blank state, overseas, unmapped state", () => {
	assert.equal(err({ country: "India", state: "Maharashtra", pincode: "" }), "");
	assert.equal(err({ country: "India", state: "", pincode: "400001" }), "");
	assert.equal(err({ country: "United States", state: "California", pincode: "638108" }), ""); // overseas
	assert.equal(err({ country: "India", state: "Other Territory", pincode: "638108" }), ""); // not in map
	assert.equal(err({}), "");
});

test("the bare 'Lakshadweep' alias is canonicalized so the pincode check runs (review D2)", () => {
	assert.equal(err({ country: "India", state: "Lakshadweep", pincode: "682555" }), ""); // 682 in range
	assert.match(
		err({ country: "India", state: "Lakshadweep", pincode: "700001" }),
		/postal code/i
	); // 700 out
});

test("the pincode check keys on the state name, not the GSTIN", () => {
	// mirrors IC's validate_pincode (address.state); a pincode that matches the state passes regardless of GSTIN
	assert.ok(pincodeMatchesState("560001", "Karnataka")); // 560 in Karnataka
	assert.ok(!pincodeMatchesState("638108", "Karnataka")); // 638 is Tamil Nadu, not Karnataka
});

test("a crafted state name can't resolve via the prototype chain (hardening)", () => {
	assert.equal(stateCodeFromName("__proto__"), null);
	assert.equal(stateCodeFromName("constructor"), null);
	assert.equal(stateCodeFromName("hasOwnProperty"), null);
	assert.equal(err({ country: "India", state: "__proto__", pincode: "400001" }), "");
});

test("PARITY GUARD: the self-contained tables match India Compliance's exactly", () => {
	// review D4: the SPA carries its OWN copy of the GST tables (no India Compliance dependency). This
	// EXHAUSTIVE assertion forces any future edit to STATE_PINCODE_MAPPING / STATE_BY_CODE through a visible
	// diff, so a transcription slip in any of the 37 entries fails CI instead of drifting silently from the
	// control plane. Expected values are India Compliance's authoritative STATE_PINCODE_MAPPING + STATE_NUMBERS.
	assert.deepEqual(STATE_PINCODE_MAPPING, {
		"Jammu and Kashmir": [180, 194],
		"Himachal Pradesh": [171, 177],
		Punjab: [140, 160],
		Chandigarh: [
			[140, 140],
			[160, 160],
		],
		Uttarakhand: [244, 263],
		Haryana: [121, 136],
		Delhi: [110, 110],
		Rajasthan: [301, 345],
		"Uttar Pradesh": [201, 285],
		Bihar: [800, 855],
		Sikkim: [737, 737],
		"Arunachal Pradesh": [790, 792],
		Nagaland: [797, 798],
		Manipur: [795, 795],
		Mizoram: [796, 796],
		Tripura: [799, 799],
		Meghalaya: [793, 794],
		Assam: [781, 788],
		"West Bengal": [700, 743],
		Jharkhand: [813, 835],
		Odisha: [751, 770],
		Chhattisgarh: [490, 497],
		"Madhya Pradesh": [450, 488],
		Gujarat: [360, 396],
		"Dadra and Nagar Haveli and Daman and Diu": [
			[362, 362],
			[396, 396],
		],
		Maharashtra: [400, 445],
		Karnataka: [560, 591],
		Goa: [403, 403],
		"Lakshadweep Islands": [682, 682],
		Kerala: [670, 695],
		"Tamil Nadu": [600, 643],
		Puducherry: [
			[533, 533],
			[605, 605],
			[607, 607],
			[609, 609],
			[673, 673],
		],
		"Andaman and Nicobar Islands": [744, 744],
		"Andhra Pradesh": [500, 535],
		Telangana: [
			[500, 509],
			[518, 518],
			[533, 533],
		],
		Ladakh: [
			[180, 180],
			[181, 181],
			[184, 184],
			[190, 191],
			[194, 194],
		],
	});
	assert.deepEqual(STATE_BY_CODE, {
		"01": "Jammu and Kashmir",
		"02": "Himachal Pradesh",
		"03": "Punjab",
		"04": "Chandigarh",
		"05": "Uttarakhand",
		"06": "Haryana",
		"07": "Delhi",
		"08": "Rajasthan",
		"09": "Uttar Pradesh",
		10: "Bihar",
		11: "Sikkim",
		12: "Arunachal Pradesh",
		13: "Nagaland",
		14: "Manipur",
		15: "Mizoram",
		16: "Tripura",
		17: "Meghalaya",
		18: "Assam",
		19: "West Bengal",
		20: "Jharkhand",
		21: "Odisha",
		22: "Chhattisgarh",
		23: "Madhya Pradesh",
		24: "Gujarat",
		26: "Dadra and Nagar Haveli and Daman and Diu",
		27: "Maharashtra",
		29: "Karnataka",
		30: "Goa",
		31: "Lakshadweep Islands",
		32: "Kerala",
		33: "Tamil Nadu",
		34: "Puducherry",
		35: "Andaman and Nicobar Islands",
		36: "Telangana",
		37: "Andhra Pradesh",
		38: "Ladakh",
		97: "Other Territory",
	});
});

test("multi-range states honor every disjoint range", () => {
	assert.ok(pincodeMatchesState("140001", "Chandigarh")); // [140,140]
	assert.ok(pincodeMatchesState("160001", "Chandigarh")); // [160,160]
	assert.ok(!pincodeMatchesState("150001", "Chandigarh")); // gap between them
	assert.ok(pincodeMatchesState("605001", "Puducherry")); // one of five
	assert.ok(pincodeMatchesState("500001", "Telangana")); // [500,509]
	assert.ok(pincodeMatchesState("194101", "Ladakh")); // [194,194]
	assert.ok(pincodeMatchesState("500001", "Andhra Pradesh")); // overlaps Telangana (500-535)
});
