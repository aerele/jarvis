// Display only: accounting calculations and rounding belong to the server.
const DECIMAL = /^-?\d+(?:\.\d+)?$/;

// The one sign reader: -1, 0 or 1 for a plain decimal string, null for anything else.
export function sign(value) {
	if (typeof value !== "string" || !DECIMAL.test(value)) return null;
	return /[1-9]/.test(value) ? (value.startsWith("-") ? -1 : 1) : 0;
}

function decimal(value, precision) {
	if (sign(value) === null) return null;
	const [integer, fraction = ""] = value.replace(/^-/, "").split(".");
	const digits = precision ?? fraction.length;
	if (!Number.isInteger(digits) || digits < 0 || digits > 9 || fraction.length > digits)
		return null;
	return {
		integer: BigInt(integer).toString(),
		fraction: fraction.padEnd(digits, "0"),
		precision: digits,
		negative: sign(value) === -1,
	};
}

function symbol(currency) {
	try {
		return new Intl.NumberFormat(currency === "INR" ? "en-IN" : "en-US", {
			style: "currency",
			currency,
		})
			.formatToParts(0)
			.find((part) => part.type === "currency").value;
	} catch {
		return currency + " ";
	}
}

export function exactMoney(value, currency, precision) {
	if (typeof currency !== "string" || !currency || currency.length > 12) return "—";
	const parsed = decimal(value, precision);
	if (!parsed) return "—";
	const grouped = new Intl.NumberFormat(currency === "INR" ? "en-IN" : "en-US", {
		maximumFractionDigits: 0,
	}).format(BigInt(parsed.integer));
	return `${parsed.negative ? "-" : ""}${symbol(currency)}${grouped}${
		parsed.precision ? "." + parsed.fraction : ""
	}`;
}

export function compactMoney(value, currency, precision, maxLength = 14) {
	if (!Number.isInteger(maxLength) || maxLength < 4 || maxLength > 14) maxLength = 14;
	const full = exactMoney(value, currency, precision);
	const parsed = decimal(value, precision);
	if (!parsed || full === "—") return full;
	const narrow = maxLength < 14;
	const trimmed = narrow ? full.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "") : full;
	if (trimmed.length <= maxLength) return trimmed;
	const scales =
		currency === "INR"
			? [[7, "Cr"], [5, "L"], ...(narrow ? [[3, "K"]] : [])]
			: [
					[9, "B"],
					[6, "M"],
					[3, "K"],
			  ];
	const scale = scales.find(([digits]) => parsed.integer.length > digits);
	if (!scale) {
		return narrow && parsed.integer.length >= 3 ? trimmed.replace(/\.\d+$/, "") : trimmed;
	}
	const [digits, suffix] = scale;
	const split = parsed.integer.length - digits;
	const prefix = `${parsed.negative ? "-" : ""}${symbol(currency)}${parsed.integer.slice(
		0,
		split
	)}`;
	const fraction = parsed.integer.slice(split, split + 2).padEnd(2, "0");
	// Shortened display retains the exact amount in the caller's accessible name.
	if (!narrow) return `${prefix}.${fraction}${suffix}`;
	for (let places = 2; places >= 0; places--) {
		const tail = fraction.slice(0, places).replace(/0+$/, "");
		const compact = `${prefix}${tail ? "." + tail : ""}${suffix}`;
		if (compact.length <= maxLength) return compact;
	}
	return `${prefix}${suffix}`;
}

// Compact signed net for calendar cells: +₹44.2k, −₹1.0L. The unit follows the
// template: chosen from the unrounded value, so 99950 reads ₹100.0k, not ₹1.0L.
export function netLabel(value, currency = "INR") {
	const parsed = decimal(value);
	if (!parsed) return "—";
	const units = BigInt(parsed.integer + parsed.fraction);
	const scale = NET_SCALES.find(
		({ digits }) => units >= 10n ** BigInt(digits + parsed.precision)
	);
	const compact = scale
		? roundedQuotient(units, parsed.precision + scale.digits, 1) + scale.suffix
		: roundedQuotient(units, parsed.precision, 0);
	const prefix = parsed.negative ? "−" : units > 0n ? "+" : "";
	return `${prefix}${symbol(currency)}${compact}`;
}

// Whole-unit amount with grouping; rounds half away from zero on the decimal string.
export function shortMoney(value, currency = "INR") {
	const parsed = decimal(value);
	if (!parsed || typeof currency !== "string" || !currency || currency.length > 12) return "—";
	const units = BigInt(parsed.integer + parsed.fraction);
	const whole = BigInt(roundedQuotient(units, parsed.precision, 0));
	const grouped = new Intl.NumberFormat(currency === "INR" ? "en-IN" : "en-US", {
		maximumFractionDigits: 0,
	}).format(whole);
	return `${parsed.negative ? "-" : ""}${symbol(currency)}${grouped}`;
}

// Headline amount: U+2212 for negatives, optional "+" for positives (month net).
export function signedMoney(value, currency, precision, { plus = false } = {}) {
	const parsed = decimal(value, precision);
	if (!parsed) return "Unavailable";
	const unsigned = exactMoney(value.replace(/^-/, ""), currency, precision);
	if (unsigned === "—") return "Unavailable";
	if (parsed.negative) return "−" + unsigned;
	return plus && /[1-9]/.test(parsed.integer + parsed.fraction) ? "+" + unsigned : unsigned;
}

// Exact and signed formatters bound to one snapshot's currency and precision.
export function snapshotMoney(snapshot) {
	const { currency } = snapshot;
	const precision = snapshot.projection?.precision;
	return {
		exact: (value) => exactMoney(value, currency, precision),
		signed: (value, options) => signedMoney(value, currency, precision, options),
	};
}

const NET_SCALES = [
	{ digits: 7, suffix: "Cr" },
	{ digits: 5, suffix: "L" },
	{ digits: 3, suffix: "k" },
];

// units / 10^shift as a decimal string with `places` decimals, half away from zero.
function roundedQuotient(units, shift, places) {
	const divisor = 10n ** BigInt(shift);
	const scaled = units * 10n ** BigInt(places);
	const rounded = (scaled * 2n + divisor) / (divisor * 2n);
	if (!places) return rounded.toString();
	const digits = rounded.toString().padStart(places + 1, "0");
	return `${digits.slice(0, -places)}.${digits.slice(-places)}`;
}
