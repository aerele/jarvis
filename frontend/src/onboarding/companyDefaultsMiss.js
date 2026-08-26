// Whether a getCompanyOnboardingDefaults rejection is the EXPECTED "no such
// company" case, not a defect. fetchCompanyDefaults (OnboardingView.vue) fires
// on every keystroke of the Company field, debounced but still constant, and
// most of those keystrokes name no Company yet - the backend
// (onboarding.get_company_onboarding_defaults) answers that with a real 4xx
// and {ok:false, error:{code:"COMPANY_DEFAULTS_NOT_FOUND"}}, deliberately, the
// same way for a blank/unresolved Company as for a genuinely custom name on a
// frappe-only bench with no Company doctype (see C01-1 in onboarding.py). None
// of that is worth a console/telemetry error on every keystroke; only
// COMPANY_DEFAULTS_FORBIDDEN and real failures (network, 500s) still are.
//
// Pure, no Vue, no `@/` alias - node --test runs it standalone, same reasoning
// as paymentCodec.js and gstin.js for keeping wire-shape decoding out of the
// SFC.
//
// The backend never raises for this case, so frappe-ui's `call` (api.js) still
// throws (the HTTP status is a real 4xx): the thrown Error's `.status` is that
// status, and its `.messages` array carries the {ok:false, error:{code,...}}
// envelope untouched as an OBJECT - `call`'s `JSON.parse` attempt on a
// non-string fails and leaves it as-is. Same wire shape filterModel.js's
// envelopeIn/filterErrorInfo reads for list_filter_* errors.

function envelopeCode(candidate) {
	if (!candidate || typeof candidate !== "object") return "";
	const error = candidate.error;
	return error && typeof error === "object" && typeof error.code === "string" ? error.code : "";
}

export function isExpectedCompanyDefaultsMiss(e) {
	if (!e) return false;
	if (e.status === 404) return true;
	const candidates = [e, ...(Array.isArray(e.messages) ? e.messages : [])];
	return candidates.some(
		(candidate) => envelopeCode(candidate) === "COMPANY_DEFAULTS_NOT_FOUND"
	);
}
