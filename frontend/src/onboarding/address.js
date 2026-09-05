// Address consistency for an India buyer: State, GSTIN and Pincode must agree. Mirrors the admin control
// plane's billing._validate_billing_state (gst_place_of_supply.py), which itself mirrors India Compliance's
// validate_pincode — so the SPA blocks the same bad address the plane rejects and the books would REVIEW,
// and the customer is told BEFORE checkout instead of after.
//
// Pure module — no Vue, no `@/` alias, no imports at all — so `node --test` runs it standalone and the two
// sides never drift on what "consistent" means. SELF-CONTAINED: it carries its OWN copy of the GST state and
// pincode tables and does NOT depend on India Compliance being installed anywhere (a jarvis tenant may not
// have IC). Keep the tables in lockstep with the plane (which carries the same copy).

// GST state/UT code -> canonical name (India Compliance STATE_NUMBERS; 97 = Other Territory).
// Exported for the exhaustive parity test (address.test.js) that guards against a transcription slip.
export const STATE_BY_CODE = {
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
};

// name (lowercased) -> code. Accepts the bare "lakshadweep" alias (the state dropdown emits it) but the
// canonical name stays "Lakshadweep Islands" — the same alias the control plane keeps. Null-prototype so a
// crafted state name ("__proto__"/"constructor") can't resolve to an inherited Object member (defensive
// hardening — the state field is a closed dropdown today, but this module must stay safe if reused).
const CODE_BY_NAME = Object.create(null);
for (const [code, name] of Object.entries(STATE_BY_CODE)) CODE_BY_NAME[name.toLowerCase()] = code;
CODE_BY_NAME["lakshadweep"] = "31";

// canonical state name -> first-3-digit pincode range(s), per the GST e-Invoice Master Codes (India
// Compliance STATE_PINCODE_MAPPING, VERBATIM). A [lo, hi] is one range; an array of them is several.
// Exported for the exhaustive parity test (address.test.js).
export const STATE_PINCODE_MAPPING = {
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
};

// A valid Indian PIN is 6 digits and cannot start with 0 (mirrors India Compliance PINCODE_FORMAT).
const PINCODE_RE = /^[1-9][0-9]{5}$/;

function isIndia(country) {
	const c = (country || "").trim().toLowerCase();
	return !c || c === "india";
}

export function stateCodeFromGstin(gstin) {
	const code = (gstin || "").trim().toUpperCase().slice(0, 2);
	return STATE_BY_CODE[code] ? code : null;
}

export function stateCodeFromName(name) {
	return CODE_BY_NAME[(name || "").trim().toLowerCase()] || null;
}

// The India Compliance canonical state name for this buyer, resolved GSTIN-FIRST (the way the books pick
// place of supply), else the state name — accepting the alias forms ("Lakshadweep" -> "Lakshadweep
// Islands"). null when neither resolves to a known GST state.
function canonicalStateName({ gstin, state }) {
	const code = stateCodeFromGstin(gstin) || stateCodeFromName(state);
	return code ? STATE_BY_CODE[code] : null;
}

// Whether the pincode's first three digits are associated with the buyer's state, mirroring IC's
// validate_pincode. True when CONSISTENT or NOT-CHECKABLE (blank/malformed pincode, a state that resolves to
// no canonical name, or a canonical state absent from the map). False ONLY on a definite out-of-range miss.
export function pincodeMatchesState(pincode, state) {
	const pin = (pincode || "").trim();
	if (!PINCODE_RE.test(pin)) return true; // blank/malformed: a separate check, not a consistency question
	const canonical = canonicalStateName({ state }); // keyed on the STATE NAME, mirroring IC's validate_pincode
	let ranges = STATE_PINCODE_MAPPING[canonical];
	if (!ranges) return true; // state not in the map (some UTs / Other Territory) — mirrors IC's own skip
	if (typeof ranges[0] === "number") ranges = [ranges]; // normalize a single [lo, hi] to a list of ranges
	const firstThree = parseInt(pin.slice(0, 3), 10);
	return ranges.some(([lo, hi]) => lo <= firstThree && firstThree <= hi);
}

/**
 * The GSTIN-vs-state message (surfaced on the GSTIN field), or "". Fires only when BOTH the GSTIN state code
 * and the state resolve to a known code and they differ — a valid 99/25/28 GSTIN has no known canonical
 * state and is never flagged (review D3). Overseas / blank inputs pass.
 */
export function gstinStateError({ country, state, gstin } = {}) {
	if (!isIndia(country)) return "";
	const g = stateCodeFromGstin(gstin);
	const n = stateCodeFromName(state);
	return g && n && g !== n ? "The GSTIN's state code doesn't match the state you selected." : "";
}

/**
 * The pincode-vs-state message (surfaced on the pincode field), or "". Rejects a malformed pincode (6-digit,
 * no leading zero) and a first-3-digit out-of-range for the state. Overseas / blank pincode or state pass.
 */
export function pincodeStateError({ country, state, pincode, gstin } = {}) {
	if (!isIndia(country)) return "";
	const pin = (pincode || "").trim();
	if (pin && !PINCODE_RE.test(pin))
		return "Postal code must be a 6-digit number and can't start with 0.";
	if (pin && (state || "").trim() && !pincodeMatchesState(pin, state))
		return "The postal code's first 3 digits don't match your state.";
	return "";
}

/**
 * The single customer-facing sentence for what is inconsistent across {country, state, pincode, gstin}, or ""
 * when they agree / the check does not apply. Composes the two field-level checks (GSTIN-vs-state first).
 * Mirrors the control plane's rejects by DEFECT CLASS, just before checkout instead of after.
 */
export function addressConsistencyError(o = {}) {
	return gstinStateError(o) || pincodeStateError(o);
}
