// Display only: accounting calculations and rounding belong to the server.
function decimal(value, precision) {
	if (typeof value !== "string" || !/^-?\d+(?:\.\d+)?$/.test(value)) return null;
	const negative = value.startsWith("-");
	const [integer, fraction = ""] = value.replace(/^-/, "").split(".");
	const digits = precision ?? fraction.length;
	if (!Number.isInteger(digits) || digits < 0 || digits > 9 || fraction.length > digits)
		return null;
	return {
		integer: BigInt(integer).toString(),
		fraction: fraction.padEnd(digits, "0"),
		precision: digits,
		negative: negative && /[1-9]/.test(integer + fraction),
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

export function compactMoney(value, currency, precision) {
	const full = exactMoney(value, currency, precision);
	const parsed = decimal(value, precision);
	if (!parsed || full === "—" || full.length <= 14) return full;
	const scales =
		currency === "INR"
			? [
					[7, "Cr"],
					[5, "L"],
			  ]
			: [
					[9, "B"],
					[6, "M"],
					[3, "K"],
			  ];
	const scale = scales.find(([digits]) => parsed.integer.length > digits);
	if (!scale) return full;
	const [digits, suffix] = scale;
	const split = parsed.integer.length - digits;
	// Shortened display retains the exact amount in the caller's accessible name.
	return `${parsed.negative ? "-" : ""}${symbol(currency)}${parsed.integer.slice(
		0,
		split
	)}.${parsed.integer.slice(split, split + 2).padEnd(2, "0")}${suffix}`;
}
