"""RFC 8707 canonical resource URI - the value sent as the ``resource`` param
and the value protected-resource metadata's own ``resource`` field must match.
Pure, no I/O.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def canonical_resource(base_url: str) -> str:
	"""Lowercase scheme + host, drop a trailing slash on the path (so
	``https://api.example.com/mcp/`` becomes ``https://api.example.com/mcp``),
	and reject a URL carrying a fragment or missing a scheme. The path's own
	case is left untouched - RFC 8707 canonicalization only calls out
	scheme/host and the trailing slash, and a path IS allowed to be
	case-sensitive on the server."""
	parsed = urlsplit(base_url)
	if not parsed.scheme:
		raise ValueError("base_url must be an absolute URL with a scheme.")
	if parsed.fragment:
		raise ValueError("base_url must not contain a fragment.")

	path = parsed.path or ""
	if path == "/":
		path = ""
	elif path.endswith("/"):
		path = path.rstrip("/")

	return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))
