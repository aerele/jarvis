/**
 * Brand primitives shared by every surface that draws the Jarvis spark.
 *
 * The glyph was previously a copy-pasted path literal in JarvisMark.vue and in
 * the onboarding neural-net canvas, and JvSpinner would have made a third copy.
 * Three hand-maintained copies of a brand glyph is three chances for a brand
 * refresh to land in two of them, so the path lives here once and every surface
 * imports it.
 *
 * Colours are deliberately NOT here. They already have a single source of truth
 * as CSS custom properties (--brand-1 / --brand-2 / --brand-grad in main.css),
 * and duplicating them as JS constants would create exactly the drift this
 * module exists to prevent. The one exception is the canvas in
 * SetupNeuralNet.vue, which cannot read a gradient from CSS and documents its
 * own literals as theme-invariant.
 */

/**
 * The Jarvis four-point spark, drawn against a 24x24 viewBox.
 * Consumers set their own fill; the path carries no colour of its own.
 */
export const BRAND_STAR_PATH = "M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z";
