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


def resource_covers(declared: str, requested: str) -> bool:
	"""True when a protected-resource ``resource`` declaration (``declared``)
	describes ``requested``: the same canonical URI, or a same-origin path
	PREFIX of it at a segment boundary - ``https://mcp.example.com`` covers
	``https://mcp.example.com/mcp``, ``https://mcp.example.com/mc`` and
	``https://mcp.example.com/other`` do not, and another origin never does.
	A query on either side has to match exactly (a prefix says nothing about
	it). Raises ``ValueError`` for a URL :func:`canonical_resource` rejects."""
	d = urlsplit(canonical_resource(declared))
	r = urlsplit(canonical_resource(requested))
	if (d.scheme, d.netloc) != (r.scheme, r.netloc):
		return False
	if d.query != r.query:
		return False
	if d.path == r.path:
		return True
	return not d.path or r.path.startswith(d.path + "/")
