"""Best-effort activity trail for the Agents Marketplace.

``log_activity`` writes ONE ``Jarvis Agent Activity`` row per lifecycle event
(install / uninstall / enable / disable / schedule / config / run
transitions). The doctype is deliberately LINK-FREE — Data snapshots of the
slug / installation / run names — because the uninstall cascade hard-deletes
installations, runs and findings, and the activity history must survive those
deletes without ever raising LinkExistsError. Every write is best-effort: a
failed insert lands in the Error Log and is swallowed, never allowed to break
the operation it narrates.
"""

import frappe

ACTIVITY = "Jarvis Agent Activity"


def log_activity(*, agent, agent_title, installation, action, detail=None, run=None, owner=None):
	"""Insert one activity row AS the current session user (the feed is
	owner-scoped via ``if_owner`` read, exactly like Jarvis Agent Run). Pass
	``owner`` to attribute the row to a specific user instead — e.g. the
	installation owner when the caller runs as Administrator (the scheduler) or
	as a System Manager acting on someone else's install. Never raises — any
	failure is logged server-side and the caller proceeds."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"agent": agent or "",
				"agent_title": agent_title or "",
				"installation": installation or "",
				"action": action,
				"run": run or "",
				"detail": detail or "",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		# insert() stamps owner = session.user (the scheduler runs as Administrator,
		# an admin acting on someone else's install is a System Manager), so an
		# explicit owner is pinned AFTER insert — mirroring _reassign_owner on the
		# Run/Finding rows — else the owner-scoped (if_owner) feed would misattribute
		# every scheduler / cross-user row to Administrator.
		if owner and owner != doc.owner:
			frappe.db.set_value(ACTIVITY, doc.name, "owner", owner, update_modified=False)
	except Exception:
		try:
			frappe.log_error(
				title="Jarvis: agent activity log failed",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
