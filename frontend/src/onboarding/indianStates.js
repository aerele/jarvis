// The GST states/UTs, mirroring India Compliance's STATE_NUMBERS so the checkout
// State picker offers exactly the values the books side (jarvis_billing) can map
// to a place-of-supply code. Order follows the GST state-code sequence.
//
// State is a Select ONLY when Country is India (place of supply is the buyer's
// state); for any other country it is a free-text region and India Compliance
// treats the buyer as Overseas.

export const INDIA = "India";

export const INDIAN_STATES = [
	"Jammu and Kashmir",
	"Himachal Pradesh",
	"Punjab",
	"Chandigarh",
	"Uttarakhand",
	"Haryana",
	"Delhi",
	"Rajasthan",
	"Uttar Pradesh",
	"Bihar",
	"Sikkim",
	"Arunachal Pradesh",
	"Nagaland",
	"Manipur",
	"Mizoram",
	"Tripura",
	"Meghalaya",
	"Assam",
	"West Bengal",
	"Jharkhand",
	"Odisha",
	"Chhattisgarh",
	"Madhya Pradesh",
	"Gujarat",
	"Dadra and Nagar Haveli and Daman and Diu",
	"Maharashtra",
	"Karnataka",
	"Goa",
	"Lakshadweep",
	"Kerala",
	"Tamil Nadu",
	"Puducherry",
	"Andaman and Nicobar Islands",
	"Telangana",
	"Andhra Pradesh",
	"Ladakh",
	"Other Territory",
];

const _STATE_SET = new Set(INDIAN_STATES.map((s) => s.toLowerCase()));

// A short, curated country list — India first (the common case), then the
// markets Jarvis sells into. "Other" lets a buyer name anything; the books side
// only distinguishes India (domestic GST) from everything else (Overseas).
export const COUNTRIES = [
	"India",
	"United States",
	"United Kingdom",
	"United Arab Emirates",
	"Singapore",
	"Australia",
	"Canada",
	"Germany",
	"Other",
];

export function isIndia(country) {
	return (country || "").trim().toLowerCase() === INDIA.toLowerCase();
}

export function isValidIndianState(state) {
	return _STATE_SET.has((state || "").trim().toLowerCase());
}
