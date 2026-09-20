"""Durable append-only audit sink for agent ERP writes (act-now #3).

Written best-effort from the write choke-point (``api._dispatch_and_wrap``) plus
the ``dismiss_tool`` discard path. Metadata only — never conversation content —
so leaking ``tool_args``/``tool_result``/``content``/``error`` is structurally
impossible (the doctype has no columns for them). The subject can't delete rows
(no role holds create/write/delete on ``Jarvis Agent Write``).

Two transactional invariants, both from plan-check:
- Recorded ONLY on the success + known-failure branches of ``_dispatch_and_wrap``
  (which return normally, so Frappe commits them at end-of-request) — never the
  re-raise/500 branch, whose full rollback would drop the row anyway.
- The insert is wrapped in its OWN savepoint so a failed audit insert can't
  poison the tool's transaction, and it NEVER commits (keeps ``FrappeTestCase``'s
  class-level rollback isolation intact).
"""

import frappe

_OUTCOMES = {"applied", "failed", "discarded"}


def record_write(
	actor,
	tool,
	args,
	result,
	outcome,
	provenance,
	provenance_name="",
	conversation="",
	model="",
):
	"""Insert one metadata-only ``Jarvis Agent Write`` row for an agent write.

	Best-effort: never raises and never commits, so it is safe to call from
	inside a tool's request without risking the tool's own transaction. A bulk
	call collapses to ONE row carrying ``bulk_count`` (the batch card is the
	human checkpoint, not N audit rows). Returns the new row name, or ``None``
	when anything went wrong (already logged)."""
	try:
		from jarvis.api import _BULK_ARG_KEYS, _bulk_len, _is_bulk_call
		from jarvis.audit import _ref

		if outcome not in _OUTCOMES:
			outcome = "applied"
		a = args if isinstance(args, dict) else {}

		# _ref returns a 3-tuple (doctype, name, method); we keep the first two.
		ref_doctype = ref_name = ""
		try:
			ref_doctype, ref_name, _method = _ref(a, result if isinstance(result, dict) else {})
		except Exception:
			pass

		bulk_count = 0
		if _is_bulk_call(a):
			# One row per bulk call: the target doctype + N, blank ref_name (a batch
			# has no single target). Count via the authoritative batch-key helper,
			# never "the first list arg" (which could catch a fields/filters list).
			bulk_count = _bulk_len(a)
			if not ref_doctype:
				items = next((a[k] for k in _BULK_ARG_KEYS if isinstance(a.get(k), list) and a[k]), [])
				if items and isinstance(items[0], dict):
					ref_doctype = items[0].get("doctype") or ""
			ref_name = ""

		actor = actor or frappe.session.user
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Write",
				"actor": actor,
				"actor_name": frappe.get_cached_value("User", actor, "full_name") or actor,
				"tool": tool,
				"outcome": outcome,
				"provenance": provenance or "chat",
				"provenance_name": provenance_name or "",
				"ref_doctype": ref_doctype or "",
				"ref_name": ref_name or "",
				"bulk_count": bulk_count,
				"conversation": conversation or "",
				"model": model or "",
				"at": frappe.utils.now(),
			}
		)

		sp = f"agent_audit_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp)
		try:
			doc.insert(ignore_permissions=True)  # server-authored; no role has create perm
		except Exception:
			frappe.db.rollback(save_point=sp)  # recover the outer txn; never poison the tool write
			raise
		return doc.name
	except Exception:
		frappe.logger("jarvis.agent_audit").error("agent-write audit failed", exc_info=True)
		return None
