"""JF-017: mark every PRE-EXISTING ``Jarvis Agent Run`` as a legacy capability run.

From this deploy on, ``agent_scheduler._launch_audit`` snapshots the run's
capability contract (``tools_allow_json`` / ``capability_nature`` /
``capability_writes_json``) onto the row and ``jarvis.api`` refuses any delegate
tool call outside it. Rows that already exist carry no snapshot, so without this
patch an in-flight run would be refused every tool the moment the code lands —
the guard would fail closed on runs it was never meant to judge.

Stamping them ``legacy`` says so explicitly: no tools_allow gate, and the write
caps keep resolving nature/writes from the live listing, exactly the regime those
runs launched under. It is a one-shot marker, never re-applied — a run created
AFTER this patch either carries a real snapshot or is refused (a blank contract on
a post-cutover row means something bypassed ``_launch_audit``).

The cutover instant itself is this patch's own ``Patch Log`` row: a blank-contract
run created before it (one that raced the deploy — new code serving before migrate
finished) is still granted the legacy fallback, one created after it is not. See
``jarvis.tools._delegate_capability``.

RAW SQL, not ``doc.save()``: the controller stamps these fields immutable, and a
per-row ORM save over the whole run history would be slow and would re-run
validation against rows the migrate has not finished touching.
"""

import frappe

RUN = "Jarvis Agent Run"


def execute():
	# The column exists after post_model_sync; guard anyway so a partial deploy
	# state can never 500 the patch.
	if not frappe.db.has_column(RUN, "capability_contract"):
		return
	frappe.db.sql(
		"""update `tabJarvis Agent Run`
		   set capability_contract = 'legacy'
		   where capability_contract is null or capability_contract = ''"""
	)
	frappe.db.commit()
