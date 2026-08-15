// Shared logged-in-user display helpers. Single source for AppSidebar + ChatView
// so the avatar/name derivation (and its cookie-decoding edge cases) live in one
// place instead of being copy-pasted and drifting.

export function readCookie(name) {
	// URLSearchParams already percent-decodes the value (handles %25, %20, %2B).
	// Do NOT decodeURIComponent the result again - a display name containing a
	// literal '%' is stored as '%25', URLSearchParams turns it back into a bare
	// '%', and a second decodeURIComponent('%…') throws URIError, blanking every
	// page that embeds the sidebar.
	return new URLSearchParams(document.cookie.split("; ").join("&")).get(name) || "";
}

export function displayName(sessionUser) {
	return readCookie("full_name") || sessionUser || "User";
}

export function initialsOf(name) {
	// Array.from iterates by code point, so an emoji / astral-plane first
	// character yields a whole glyph instead of a lone surrogate half (�).
	return (
		(name || "")
			.trim()
			.split(/\s+/)
			.map((w) => Array.from(w)[0] || "")
			.slice(0, 2)
			.join("")
			.toUpperCase() || "U"
	);
}
