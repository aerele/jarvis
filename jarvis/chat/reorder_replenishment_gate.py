"""Compatibility names for Reorder; the shared closure owns all authority."""

from jarvis.chat import agent_closure

REORDER_AGENT = "reorder-replenishment-operator"
RUN_UNAVAILABLE = agent_closure._RUN_UNAVAILABLE
assert_listing_status = agent_closure.assert_listing_status
delegate_tool_denial = agent_closure.delegate_tool_denial


def is_reorder(agent):
	return (agent or "").strip() == REORDER_AGENT


def direct_write_refusal(operation, doctype):
	return agent_closure.direct_write_refusal(REORDER_AGENT, operation, doctype)
