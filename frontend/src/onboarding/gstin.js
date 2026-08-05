// GSTIN (Indian GST identification number) validation: shape + state code +
// mod-36 checksum. Pure module - no Vue, no `@/` alias, no imports at all - so
// `node --test` runs it standalone and the admin control plane's own GSTIN
// rules (billing.gstin) never drift from what this SPA checks client-side.
// GSTIN is OPTIONAL on the Details step: a blank value is never an error here,
// only a malformed non-blank one is.

const ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
// 2-digit state code, 5-letter PAN prefix, 4-digit PAN sequence, 1-letter PAN
// check char, 1 entity code (1-9 or A-Z, never 0), literal "Z", 1 checksum char.
const SHAPE_RE = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/;

// GST state/UT codes run 01-38; 97 (Other Territory) and 99 (Centre jurisdiction
// / foreign) are also issued. Mirrors the admin plane's own list so the two
// never disagree about what "unrecognized state code" means.
function isKnownStateCode(code) {
	const n = Number(code);
	return (n >= 1 && n <= 38) || n === 97 || n === 99;
}

// GSTIN check-digit algorithm (mod 36). Each of the first 14 characters is
// weighted 1 or 2 by its 0-based position (even -> 1, odd -> 2); each weighted
// product is folded into base-36 range (quotient + remainder) and summed; the
// check digit is (36 - sum % 36) % 36, read back through the same alphabet.
function computeChecksum(first14) {
	let sum = 0;
	for (let i = 0; i < first14.length; i++) {
		const value = ALPHABET.indexOf(first14[i]);
		const weight = i % 2 === 0 ? 1 : 2;
		const product = value * weight;
		sum += Math.floor(product / 36) + (product % 36);
	}
	const check = (36 - (sum % 36)) % 36;
	return ALPHABET[check];
}

/**
 * Full validation: length, shape, state code, and checksum, all at once. A
 * blank value is NOT a valid GSTIN (it is simply absent) - callers that treat
 * the field as optional should check for blank separately, or use gstinError.
 */
export function isValidGstin(value) {
	const v = (value || "").trim().toUpperCase();
	if (v.length !== 15) return false;
	if (!SHAPE_RE.test(v)) return false;
	if (!isKnownStateCode(v.slice(0, 2))) return false;
	return computeChecksum(v.slice(0, 14)) === v[14];
}

/**
 * The customer-facing sentence for what is wrong with `value`, or "" when it
 * is either valid or blank (GSTIN is optional on the Details step). Names the
 * specific defect so the wizard never disagrees with the admin plane's own
 * "billing.gstin is not a valid GSTIN" / "has an unrecognized state code" /
 * "failed its checksum check" rejections - it catches the same three classes,
 * just before checkout instead of after it.
 */
export function gstinError(value) {
	const v = (value || "").trim().toUpperCase();
	if (!v) return "";
	if (v.length !== 15) return "GSTIN must be 15 characters.";
	if (!SHAPE_RE.test(v)) return "That doesn't look like a GSTIN, check the letters and numbers.";
	if (!isKnownStateCode(v.slice(0, 2))) return "GSTIN has an unrecognized state code.";
	if (computeChecksum(v.slice(0, 14)) !== v[14])
		return "GSTIN doesn't check out, please look for a typo.";
	return "";
}

// A checksum-VALID example, computed (not guessed) from the same algorithm
// above, so the placeholder never shows the customer an invalid GSTIN as a
// model to copy. See gstin.test.js for the check against a hand run.
export const GSTIN_PLACEHOLDER = "33ABCDE1234F1Z" + computeChecksum("33ABCDE1234F1Z");
