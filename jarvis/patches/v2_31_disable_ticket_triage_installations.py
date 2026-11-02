"""Retire Ticket Triage through the shared close foundation.

Deployment runbook: put the site in maintenance or drain state; stop web and
background workers that can hold older Jarvis imports; run ``bench migrate``
from the new checkout; restart all workers; then Apply and verify that the fleet
roster omits this agent. Do not use this patch as a substitute for worker drain.
"""

from jarvis.chat.agent_closure import run_close_migration


def execute():
	run_close_migration("ticket-triage-operator")
