// Decisions about the mark's face, kept out of the components so bare `node --test` runs
// them. The PWA mark has fewer moods than the desktop one. `peek` is only the welcome scene's
// face (eyes, no smile): a phone has no hover and no idle blink.

export const MOODS = ["star", "happy", "upgrading", "peek"];

/** The face is showing: any mood other than the resting star. */
export function faceShown(mood) {
	return mood !== "star";
}

/** The welcome smile replaces only the resting star; the upgrade hold keeps its sleepy face. */
export function effectiveMood(mood, smiling) {
	return smiling && mood === "star" ? "happy" : mood;
}

/**
 * The mood while the welcome scene drives the mark: {face, happy} are its beats. Like the
 * smile, it only replaces the resting star; the upgrade hold keeps its own face.
 */
export function stageMood(mood, stage) {
	if (mood !== "star") return mood;
	if (stage.happy) return "happy";
	return stage.face ? "peek" : "star";
}
