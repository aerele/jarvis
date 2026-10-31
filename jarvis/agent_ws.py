"""Bench → agent container connect check.

Single use: the `Test Agent Connection` diagnostic button on Jarvis Settings (via
`jarvis.diagnostics.ping_agent`) connects to the customer's container with the paired
chat device, completes the operator-role connect handshake, and closes. The handshake
is the one chat uses, so it presents the device token with the narrow chat scopes and
never the gateway owner token or operator.admin.
"""

from jarvis.chat.agent_client import AgentSession


def ping(gateway_url: str) -> None:
	"""Connect to the agent runtime with the paired device and close.

	Raises ``AgentUnreachableError`` if the socket can't open or the handshake is
	rejected, and ``SiteIsCopyError`` on a copied site (checked by ``connect``).
	"""
	AgentSession.connect(gateway_url).close()
