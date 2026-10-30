// Decisions about the mark's face, kept out of the components so bare `node --test` runs
// them. The PWA mark has three moods; the desktop one has more.

export const MOODS = ["star", "happy", "upgrading"];

/** The face is showing: a mood other than the resting star, or a peek. */
export function faceShown(mood, peek) {
	return mood !== "star" || !!peek;
}

/** The welcome smile replaces only the resting star; the upgrade hold keeps its sleepy face. */
export function effectiveMood(mood, smiling) {
	return smiling && mood === "star" ? "happy" : mood;
}

/** How the worn item reacts (data-state on the layer): a smile pops it, anything else peeks. */
export function layerState(mood) {
	return mood === "happy" ? "happy" : "peek";
}

/**
 * A custom agent name or a logo means the product is not presented as Jarvis: the same
 * rule as the server's white_label(). The ordinary words still show; the face's item does not.
 */
export function isWhitelabelBrand(agentName, logoUrl, defaultName = "Jarvis") {
	return agentName !== defaultName || !!logoUrl;
}
