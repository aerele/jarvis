"""Jarvis Agent Activity DocType controller.

One append-only lifecycle event per Agents-Marketplace action (install /
uninstall / enable / disable / schedule / config / run transitions), written
best-effort by ``jarvis.chat.agent_activity.log_activity``. Every reference
field is a Data SNAPSHOT — never a Link — so the uninstall cascade (which
hard-deletes the installation, its runs and its findings) can never trip
LinkExistsError here and the history outlives the rows it narrates. Rows are
server-generated, so the ``All`` role gets ``if_owner`` READ only — the
customer sees their own feed but cannot forge or edit entries.
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisAgentActivity(NotRenamable, Document):
	pass
