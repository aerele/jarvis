"""MCP OAuth Token - one per (connector, user), holding that user's tokens for
a connector that signs in through the spec-compliant engine
(MCP_OAUTH_CLIENT_DESIGN.md section 5).

Per-user isolation is the whole point, and it is structural: the docname is
``{connector}-{user}``, so the broker - which runs as the impersonated end user
- can only ever name the row belonging to the user it is running as. There is
no query that could hand one user another's token.

Server-owned internals, same as ``MCP OAuth Client``: written by
``jarvis.connectors.mcp_oauth_store`` under ``ignore_permissions`` after the
caller-level gate on the owning ``Jarvis Connector`` row has run, and no Jarvis
User role appears in the permission rows.

``resource`` is a security field, not a record-keeping one: it pins the token to
the connector address it was issued for, so re-pointing a connector cannot
forward this token to a different host. ``mcp_oauth_store.load_live_token``
enforces it on every read.
"""

from __future__ import annotations

from frappe.model.document import Document


class MCPOAuthToken(Document):
	pass
