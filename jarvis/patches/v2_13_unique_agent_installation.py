"""Add the composite unique index on (owner, agent) to
`tabJarvis Agent Installation`, backing the check-then-act uniqueness in
jarvis_agent_installation._validate_unique_per_owner (#460).

Root cause this closes: uniqueness of (owner, agent) was enforced ONLY by
`frappe.db.exists` + `frappe.throw`, in the controller and again in
`agents_api.install_agent`. The doctype is `autoname: hash` /
`naming_rule: Random` and declares no `unique` field, so nothing at the DB
level backstopped it: two concurrent installs (a double click, or two
gunicorn workers) both saw no clash under REPEATABLE READ and both committed
under distinct hash names. The duplicate inflated `install_count` and made a
single uninstall leave a phantom row behind, so the agent still rendered as
installed.

Existing duplicates are MERGED, not destroyed, before the constraint is added
(ALTER TABLE would otherwise hard-fail on any bench already carrying rows from
exactly the race this closes). See `_merge_duplicate_installs` for which row
survives and why.

The index also lives in `on_doctype_update()`, which is what gives it to a
FRESH install: `install_app` runs with `set_as_patched=True`, so every patch in
patches.txt is marked done without running. Conversely `bench migrate`
re-imports a DocType only when its `.json` changes, and #460 is a `.py`-only
change, so on an EXISTING site the hook never fires by itself -- hence the
explicit `reload_doc` here.
"""

import frappe

INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
PROVENANCE = "Jarvis Agent Provenance Event"
ACTIVITY = "Jarvis Agent Activity"


def _keeper_rank(row: dict) -> tuple:
	"""Sort key for picking which duplicate SURVIVES (lowest tuple wins).

	Ordered by how expensive the state is to reconstruct, not by age:

	1. ``activation_state == "live"`` -- reaching live costs a named reviewer's
	   PP-4 sign-off and a slot under the PP-6 activation ceiling. Silently
	   demoting a live capability is the single worst outcome here.
	2. ``enabled`` -- a running agent must not silently stop running.
	3. ``schedule_enabled`` -- likewise for its cadence.
	4. run count -- a pure tiebreak. History is NOT what decides the winner,
	   because history is preserved either way: every loser's runs, provenance
	   events and activity rows are re-pointed at the keeper below.
	5. oldest ``creation`` -- of two otherwise identical rows the first is the
	   real install and the second is the racer.
	6. ``name`` -- so the choice is deterministic and the patch is idempotent.
	"""
	return (
		0 if (row.get("activation_state") or "shadow") == "live" else 1,
		0 if frappe.utils.cint(row.get("enabled")) else 1,
		0 if frappe.utils.cint(row.get("schedule_enabled")) else 1,
		-int(row.get("run_count") or 0),
		str(row.get("creation") or ""),
		str(row.get("name") or ""),
	)


def _merge_duplicate_installs() -> int:
	"""Collapse every duplicate (owner, agent) group onto one keeper row.

	Nothing is destroyed except the redundant installation row itself: runs,
	provenance events and activity rows belonging to a loser are re-pointed at
	the keeper first, so the customer's installation history survives the merge
	intact and the findings hanging off those runs stay reachable.

	Idempotent: a second execution finds no group with COUNT(*) > 1 and returns 0.
	"""
	dupes = frappe.db.sql(
		"""
		SELECT `owner`, `agent`
		FROM `tabJarvis Agent Installation`
		GROUP BY `owner`, `agent`
		HAVING COUNT(*) > 1
		""",
		as_dict=True,
	)
	merged = 0
	for d in dupes:
		rows = frappe.get_all(
			INSTALLATION,
			filters={"owner": d.owner, "agent": d.agent},
			fields=[
				"name",
				"creation",
				"enabled",
				"schedule_enabled",
				"activation_state",
			],
			ignore_permissions=True,
		)
		if len(rows) < 2:
			continue
		for r in rows:
			r["run_count"] = frappe.db.count(RUN, {"installation": r["name"]})
		rows.sort(key=_keeper_rank)
		keep, extras = rows[0], rows[1:]
		for extra in extras:
			frappe.db.set_value(
				RUN, {"installation": extra["name"]}, "installation", keep["name"], update_modified=False
			)
			frappe.db.set_value(
				PROVENANCE,
				{"installation": extra["name"]},
				"installation",
				keep["name"],
				update_modified=False,
			)
			# ``installation`` on the activity log is a Data field (Link-free by
			# design, so the row survives the uninstall cascade), but it is still the
			# join the agent activity feed uses.
			frappe.db.set_value(
				ACTIVITY,
				{"installation": extra["name"]},
				"installation",
				keep["name"],
				update_modified=False,
			)
			frappe.db.delete(INSTALLATION, {"name": extra["name"]})
			merged += 1
	return merged


def execute():
	if not frappe.db.table_exists(INSTALLATION):
		return
	_merge_duplicate_installs()
	frappe.db.commit()
	# Re-saving the DocType is what actually fires on_doctype_update(), which owns
	# the index. A .py-only change never triggers it on an existing site, so
	# without this migrate exits 0 and the index silently never exists.
	frappe.reload_doc("jarvis", "doctype", "jarvis_agent_installation", force=True)
	# Belt and braces: reload_doc is the canonical path, but the constraint must
	# not depend on it. add_unique no-ops when the index is already present.
	frappe.db.add_unique(INSTALLATION, ["owner", "agent"], constraint_name="owner_agent")
	frappe.db.commit()
