// The white-label rule, free of `window` so bare `node --test` can run it (branding.js reads
// the boot payload and calls this).

/**
 * A custom agent name or a logo means the product is not presented as Jarvis: the same
 * rule as the server's white_label(). The ordinary words still show; the face's item does not.
 */
export function isWhitelabelBrand(agentName, logoUrl, defaultName = "Jarvis") {
	return agentName !== defaultName || !!logoUrl;
}
