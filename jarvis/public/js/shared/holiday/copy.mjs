// The holiday words: one table for every surface (desktop now; PWA and mini chat
// later). English only, and never the product name, so a white-label site can
// still show them. Text only: this file stays in the main bundle.

// occasion -> [advance wish under the normal greeting, day heading before the name].
// An occasion is a design, except republic (the independence design, its own words).
const WORDS = {
  diwali: ["Wishing you a happy Diwali in advance!", "Happy Diwali"],
  pongal: ["Wishing you a happy Pongal in advance!", "Happy Pongal"],
  eid: ["Eid Mubarak in advance!", "Eid Mubarak"],
  christmas: ["Wishing you a merry Christmas in advance!", "Merry Christmas"],
  newyear: ["Wishing you a happy New Year in advance!", "Happy New Year"],
  onam: ["Wishing you a happy Onam in advance!", "Happy Onam"],
  holi: ["Wishing you a happy Holi in advance!", "Happy Holi"],
  independence: [
    "Wishing you a happy Independence Day in advance!",
    "Happy Independence Day",
  ],
  republic: [
    "Wishing you a happy Republic Day in advance!",
    "Happy Republic Day",
  ],
  // A birthday has no lead-up.
  birthday: [null, "Happy birthday"],
};

export const OCCASIONS = Object.keys(WORDS);

// The festive starter card for scene days (the Christmas track): [title, prompt].
// Never for the birthday, which stays private.
export const FEST = {
  diwali: ["Diwali greetings", "Draft a Diwali greeting for my customers"],
  christmas: [
    "Season's greetings",
    "Write a season's greetings email to my top customers",
  ],
  newyear: [
    "New Year greetings",
    "Write a New Year greeting email to my top customers",
  ],
  pongal: ["Pongal greetings", "Draft a Pongal greeting for my customers"],
  onam: ["Onam greetings", "Draft an Onam greeting for my customers"],
  eid: ["Eid greetings", "Draft an Eid Mubarak greeting for my customers"],
  holi: ["Holi greetings", "Draft a Holi greeting for my customers"],
  independence: [
    "Independence Day message",
    "Write an Independence Day message for my team",
  ],
  republic: [
    "Republic Day message",
    "Write a Republic Day message for my team",
  ],
};

function wordsFor(occasion) {
  // Own keys only: "toString" or "__proto__" must read as unknown.
  return typeof occasion === "string" &&
    Object.prototype.hasOwnProperty.call(WORDS, occasion)
    ? WORDS[occasion]
    : null;
}

/** "Happy Diwali, Priya". No name means no dangling comma. Null for an unknown occasion. */
export function headingFor(occasion, first) {
  const words = wordsFor(occasion);
  if (!words) return null;
  const name = typeof first === "string" ? first.trim() : "";
  return name ? `${words[1]}, ${name}` : words[1];
}

/** The advance wish shown under the normal greeting, or null (birthday, unknown). */
export function leadupLineFor(occasion) {
  const words = wordsFor(occasion);
  return words ? words[0] : null;
}
