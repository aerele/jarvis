"""MCP OAuth Client - one per connector row that signs in through the
spec-compliant engine (MCP_OAUTH_CLIENT_DESIGN.md section 5).

Server-owned internals: every field is written by
``jarvis.connectors.mcp_oauth_store`` under ``ignore_permissions`` after the
caller-level gate on the OWNING ``Jarvis Connector`` row has already run. No
Jarvis User role appears in the permission rows, so a tenant user can never
create one, point one at another connector, or read a client secret out of it -
they only ever reach it through the connector they hold permissions on.

The DocType name IS the connector's name (``autoname: field:connector``), which
is what makes "one client per connector" a database-level guarantee rather than
a convention this controller has to police.
"""

from __future__ import annotations

from frappe.model.document import Document


class MCPOAuthClient(Document):
	pass
