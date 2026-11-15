"""Real-backend flow proofs for jarvis-admin-v2#818 (sandbox-blocked attributes).

Flow step 2: a Script trigger on a real Purchase Order ``on_submit`` runs
(the fixed f-string form) and the ``.format`` form is refused at insert.
Flow step 3: the chat agent's ``create_doc`` of a Jarvis Trigger with
``.format`` is refused BEFORE a confirmation card is parked; the f-string
form parks a card.

The Purchase Order masters come from ``test_purchase_match`` (zz-apm fixtures)
and are purged on cleanup. Script execution runs with ``is_safe_exec_enabled``
patched ON so the suite passes on benches without ``server_script_enabled``.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import strip_html

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"
PENDING = "Jarvis Pending Action"
CTRL_MOD = "jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger"
EXEC_FLAG = "frappe.utils.safe_exec.is_safe_exec_enabled"

FSTRING_BODY = 'frappe.msgprint(f"PO {doc.name} submitted")'
FORMAT_BODY = 'frappe.msgprint("PO {} submitted".format(doc.name))'
TWO_FORMAT_BODY = 'frappe.msgprint("PO {} submitted".format(doc.name))\nx = "{}".format(1)'


def _trigger_doc(body: str, target: str = "Purchase Order", event: str = "on_submit") -> dict:
	return {
		"doctype": TRIGGER,
		"trigger_name": f"flow818-{frappe.generate_hash(length=8)}",
		"target_doctype": target,
		"doc_event": event,
		"action_type": "Script",
		"script_body": body,
		"enabled": 1,
	}


class _Base(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._triggers: list[str] = []
		frappe.flags._jarvis_trigger_depth = 0
		engine.clear_cache()
		self.addCleanup(self._cleanup_triggers)

	def _cleanup_triggers(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		names = set(self._triggers) | set(
			frappe.get_all(TRIGGER, filters={"trigger_name": ["like", "flow818-%"]}, pluck="name")
		)
		for name in names:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		engine.clear_cache()
		frappe.db.commit()

	def _insert_trigger(self, body: str, **kwargs):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			doc = frappe.get_doc(_trigger_doc(body, **kwargs)).insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		engine.clear_cache()
		return doc


class TestPurchaseOrderSubmitFlow(_Base):
	"""Flow step 2 (real backend): trigger on a real Purchase Order submit."""

	def setUp(self):
		super().setUp()
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest("ERPNext is required for a Purchase Order")
		from jarvis.tests import test_purchase_match as tpm

		self.tpm = tpm
		self.addCleanup(tpm._purge_ap_e2e)
		tpm._ap_e2e_fixtures()

	def _new_po(self):
		today = frappe.utils.today()
		return frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"company": self.tpm.AP_E2E_CO,
				"supplier": self.tpm.AP_E2E_SUPPLIER,
				"transaction_date": today,
				"schedule_date": today,
				"items": [{"item_code": self.tpm.AP_E2E_ITEM, "qty": 1, "rate": 10, "schedule_date": today}],
			}
		).insert(ignore_permissions=True)

	def test_f_string_trigger_runs_on_real_po_submit(self):
		trig = self._insert_trigger(FSTRING_BODY)
		po = self._new_po()
		frappe.local.message_log = []
		with patch(EXEC_FLAG, return_value=True):
			po.submit()
		po.reload()
		self.assertEqual(po.docstatus, 1)
		texts = [strip_html(str(m.get("message", ""))) for m in frappe.local.message_log]
		self.assertIn(f"PO {po.name} submitted", texts)
		rows = frappe.get_all(
			ACTIVITY,
			filters={"trigger": trig.name, "target_docname": po.name},
			fields=["status", "summary"],
		)
		self.assertEqual([r.status for r in rows], ["Success"], rows)

	def test_format_trigger_is_refused_at_insert_with_hint(self):
		before = frappe.db.count(TRIGGER, {"trigger_name": ["like", "flow818-%"]})
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError) as ctx:
				frappe.get_doc(_trigger_doc(FORMAT_BODY)).insert(ignore_permissions=True)
		text = strip_html(str(ctx.exception))
		self.assertIn("blocks: line 1", text)
		self.assertIn("f-string", text)
		self.assertEqual(frappe.db.count(TRIGGER, {"trigger_name": ["like", "flow818-%"]}), before)


class TestAgentCreateTriggerFlow(_Base):
	"""Flow step 3: the chat agent's create_doc path (api._run_tool)."""

	def _create(self, body: str) -> tuple[dict, dict]:
		spy = {}
		real = pending_confirm.mint

		def mint(**kwargs):
			token = real(**kwargs)
			spy["token"] = token
			return token

		args = {"doctype": TRIGGER, "values": _trigger_doc(body, target="ToDo", event="on_update")}
		args["values"].pop("doctype")
		with (
			patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True),
			patch("jarvis.chat.pending_confirm.mint", side_effect=mint),
			patch("jarvis.chat.events.publish_to_user") as pub,
		):
			res = api._run_tool("create_doc", json.loads(json.dumps(args)))
		spy["published"] = pub.called
		return res, spy

	def _pending_count(self):
		return frappe.db.count(PENDING)

	def test_format_script_is_refused_before_a_card_is_parked(self):
		pending_before = self._pending_count()
		res, spy = self._create(TWO_FORMAT_BODY)
		self.assertFalse(res["ok"], res)
		message = res["error"]["message"]
		self.assertNotIn("&quot;", message)
		self.assertIn("line 1", message)
		self.assertIn("line 2", message)
		self.assertIn("; ", message)
		self.assertNotIn("\n", message)
		self.assertIn("f-string", message)
		self.assertIsNone(spy.get("token"), "a token was minted")
		self.assertFalse(spy["published"], "a card was published")
		self.assertEqual(self._pending_count(), pending_before)
		self.assertFalse(frappe.db.exists(TRIGGER, {"script_body": TWO_FORMAT_BODY}))

	def test_f_string_script_parks_a_card(self):
		res, spy = self._create(FSTRING_BODY)
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(spy.get("token"), "no token was minted")
		# Parked, not executed: the trigger does not exist yet.
		self.assertFalse(frappe.db.exists(TRIGGER, {"script_body": FSTRING_BODY}))
