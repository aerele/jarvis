"""Exact identifiers used by the agent gateway and container.

These values belong to the external runtime contract, not Jarvis branding.
Keep them literal and change them only alongside a compatible runtime update.
This module has no framework dependencies so every chat transport can use it.
"""

MESSAGE_METADATA_KEY = "__openclaw"
CANVAS_ROUTE = "/__openclaw__/canvas/"
MEDIA_ROUTE = "/__openclaw__/assistant-media"
LIVE_RELOAD_ROUTE = "/__openclaw__/ws"

# Coupled to the container HOME and the control-plane /home/node egress rule.
# Coordinate changes with fleet configuration and media-path validation.
MEDIA_ROOT = "/home/node/.openclaw/media/"
