"""MCP connectors - the bench-broker plane.

An agent gets two dumb tools (call_connector, list_connector_actions); the bench
does the real MCP call under the resolved (impersonated) user, so the credential
never enters the agent container and Frappe row permissions enforce tenant
isolation for free. See ``MCP_CONNECTORS_PLAN.md`` and memory
``jarvis-mcp-connectors-design``.

Layering (import direction is one-way, leaves first):

  * ``ssrf``      - SSRF-guarded, IP-pinned HTTP seam. NO frappe import.
  * ``limits``    - circuit breaker + concurrency cap over an injectable store. NO frappe.
  * ``mcp_client``- synchronous MCP Streamable-HTTP client over ``ssrf``. NO frappe import.
  * ``broker``    - the orchestrator the tools call. This is the ONLY module here that
                    imports frappe (row resolution, credentials, cache, audit log).

The three lower modules are deliberately frappe-free so their unit tests run under a
plain ``python -m unittest`` without a bench.
"""
