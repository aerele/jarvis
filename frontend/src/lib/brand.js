/**
 * Brand primitives shared by every surface that draws the Jarvis spark.
 *
 * The glyph was previously a copy-pasted path literal in JarvisMark.vue and in
 * the onboarding neural-net canvas, and JvSpinner would have made a third copy.
 * Three hand-maintained copies of a brand glyph is three chances for a brand
 * refresh to land in two of them, so the path lives here once and every surface
 * imports it.
 *
 * Colour VALUES are deliberately NOT here. They already have a single source of
 * truth as CSS custom properties (--brand-1 / --brand-2 / --brand-grad in
 * main.css), and duplicating them as JS constants would create exactly the
 * drift this module exists to prevent. What does live here is the one helper
 * the canvases need to USE those tokens, since a canvas cannot apply a CSS
 * colour at partial opacity the way a stylesheet can.
 */

/**
 * The Jarvis four-point spark, drawn against a 24x24 viewBox.
 * Consumers set their own fill; the path carries no colour of its own.
 */
export const BRAND_STAR_PATH = "M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z";

/**
 * A hex colour at partial opacity, as an rgba() string a canvas can use.
 *
 * The canvases read their colours from CSS custom properties (the brand-asset
 * exception in design.md requires it), but a token is a flat hex and the
 * illustrations need it at many opacities for edges, halos and trails. This
 * turns one into the other.
 *
 * Lives here for the same reason BRAND_STAR_PATH does: it was written twice,
 * once in SetupNeuralNet.vue and once in PaymentConfirmingArt.vue, byte for
 * byte apart from the fallback. Two hand-maintained copies is two chances for a
 * fix to land in one of them.
 *
 * @param {string} hex - "#rgb" or "#rrggbb". Anything else uses `fallback`.
 * @param {number} a - alpha, 0 to 1.
 * @param {string} [fallback] - rgb triple to use when `hex` is unparseable.
 */
export function alphaHex(hex, a, fallback = "110,139,255") {
	let h = String(hex || "")
		.trim()
		.replace("#", "");
	if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
	const n = parseInt(h, 16);
	if (h.length !== 6 || Number.isNaN(n)) return `rgba(${fallback},${a})`;
	return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
