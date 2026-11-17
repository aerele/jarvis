// Text rules of the minimum-cash field (template L426-432). Display and validation only:
// the server stores and applies the amount.
export const FLOOR_ERROR = "Enter a non-negative amount with up to two decimal places.";
export const FLOOR_LIMIT_ERROR = "That amount is too large. Enter 1,000,000,000 or less.";
const LIMIT = 1_000_000_000;

// Commas, spaces and one leading currency sign (₹5,000) are layout, not part of the amount.
const strip = (text) =>
	String(text ?? "")
		.replaceAll(",", "")
		.trim()
		.replace(/^\p{Sc}\s*/u, "");
const DECIMAL = /^(\d+)(?:\.(\d+))?$/;

// 50000.00 / 50,000 / 050000 all read as "50000", so Save shows only for a real change.
function canonical(text) {
	const match = DECIMAL.exec(strip(text));
	if (!match) return null;
	const fraction = (match[2] || "").replace(/0+$/, "");
	return BigInt(match[1]).toString() + (fraction ? "." + fraction : "");
}

// { value } is the comma-free text to send, { error } the message to show.
export function parseFloor(text) {
	const value = strip(text);
	if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return { error: FLOOR_ERROR };
	return Number(value) <= LIMIT ? { value } : { error: FLOOR_LIMIT_ERROR };
}

export function formatFloor(saved) {
	const value = canonical(saved);
	if (value === null) return "";
	const [integer, fraction] = value.split(".");
	const grouped = new Intl.NumberFormat("en-IN").format(BigInt(integer));
	return fraction ? `${grouped}.${fraction}` : grouped;
}

export function sameFloor(text, saved) {
	if (saved == null) return strip(text) === "";
	const typed = canonical(text);
	return typed !== null && typed === canonical(saved);
}
