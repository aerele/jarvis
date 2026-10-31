// Decisions about the mark's face, kept out of the components so bare `node --test` runs
// them. The PWA mark has three moods; the desktop one has more. There is no `peek`
// (hover or idle blink) here: a phone has no hover and the hero blinks only for the smile.

export const MOODS = ["star", "happy", "upgrading"];

/** The face is showing: any mood other than the resting star. */
export function faceShown(mood) {
	return mood !== "star";
}

/** The welcome smile replaces only the resting star; the upgrade hold keeps its sleepy face. */
export function effectiveMood(mood, smiling) {
	return smiling && mood === "star" ? "happy" : mood;
}
