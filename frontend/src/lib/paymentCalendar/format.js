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
