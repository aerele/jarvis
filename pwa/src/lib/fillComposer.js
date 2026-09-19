// The starter-tap contract, as pure logic so `node --test` can assert it (the
// PWA has no component harness). A tapped starter yields the PROMPT TEXT that
// the view places in the composer. It deliberately returns text to FILL, never
// a send payload — there is no send path here; the user presses send themselves.
export function pickStarterPrompt(card) {
	return card && typeof card.prompt === "string" ? card.prompt : "";
}
