// Layered re-check, phase 2: detect a typed "surface my missing confirmation" message so
// the client can re-surface a parked card instantly (source="typed"), without a model
// round-trip. Deliberately WHOLE-MESSAGE frozenset equality (not a regex / substring /
// prefix match) so it is negation-safe by construction ("don't show it" != "show it") and
// can never fire on a prefix of a real request ("show me the sales orders"). Normalises
// case, whitespace, curly apostrophes (mobile keyboards), and one trailing punctuation run.
// Anything this narrow set misses still reaches the model, where the persona backstop
// ("show it" -> re-call the tool) handles looser phrasings. A false positive is harmless:
// the caller re-surfaces (a no-op if nothing is pending) and still sends the message.
const RECHECK_PHRASES = new Set([
  "show it",
  "show the card",
  "show the confirmation",
  "i can't see it",
  "i can't see the card",
  "i can't see the confirmation",
  "the confirmation didn't appear",
  "the card didn't appear",
]);

const MAX_LEN = 40;

export function isShowCardRequest(text) {
  if (typeof text !== "string") return false;
  const n = text
    .toLowerCase()
    .replace(/[‘’]/g, "'") // curly apostrophes -> straight
    .replace(/[.!?]+$/, "") // one trailing punctuation run
    .replace(/\s+/g, " ")
    .trim();
  if (n.length > MAX_LEN) return false; // defensive: the phrases are all short
  return RECHECK_PHRASES.has(n);
}
