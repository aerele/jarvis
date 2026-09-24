"""feat/filebox-wiki-review: review-before-landing for a File Box wiki write.

A file-box run's ``update_wiki`` is HELD as a Pending ``Jarvis Approval Request``
of source ``File Box Wiki`` (api._propose_file_box_wiki_write) instead of landing.
A Jarvis reviewer (the dropper too, when they hold a reviewer role) approves it
through approvals_api.approve_wiki_write, which replays the fenced write under the
dropper's provenance. These tests cover the reviewer gate, the claim-first race
guard, the apply-status bookkeeping, and the defense-in-depth guards that keep a
wiki proposal OFF the customer decide board.
"""

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import approvals_api

CONV = "Jarvis Conversation"
APPROVAL = "Jarvis Approval Request"

DROPPER = "wiki-dropper@test.com"
REVIEWER = "wiki-reviewer@test.com"
PLAIN = "wiki-plain@test.com"

_ARGS = {
	"slug": "party-fake-co",
	"title": "Fake Co",
	"page_type": "Reference",
	"append_md": "## Invoice INV-1\nTotal 100",
	"summary": "party page",
}

_APPLIED = [{"slug": "party-fake-co", "ok": True, "reason": "applied"}]


def _ensure_user(email: str, roles: tuple) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("User", email, "enabled", 1, update_modified=False)
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.db.commit()
	return email


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _funnel(result=None, side_effect=None):
	"""Patch the fenced funnel + the two audit sinks so the executor is hermetic."""
	with (
		patch(
			"jarvis.chat.wiki.apply_extracted_page_updates",
			return_value=result if result is not None else _APPLIED,
			side_effect=side_effect,
		) as apply,
		patch("jarvis.api.audit.record"),
		patch("jarvis.agent_audit.record_write") as board,
	):
		yield apply, board


class TestWikiWriteReviewLanding(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Dropper mirrors grant_onboarding_admin: a Jarvis Admin who is ALSO the
		# file dropper, so the reviewer role is what lets them approve.
		_ensure_user(DROPPER, ("Jarvis User", "Jarvis Admin"))
		_ensure_user(REVIEWER, ("Jarvis Admin",))
		_ensure_user(PLAIN, ("Jarvis User",))

	def tearDown(self):
		for name in frappe.get_all(CONV, filters={"title": "wiki-review test"}, pluck="name"):
			frappe.db.delete(APPROVAL, {"conversation": name})
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _conv(self, owner: str = DROPPER, **flags) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "wiki-review test"})
		doc.insert(ignore_permissions=True)
		vals = {"owner": owner, **flags}
		frappe.db.set_value(CONV, doc.name, vals, update_modified=False)
		return doc.name

	def _propose(self, conv: str, args: dict | None = None) -> str:
		"""Create a Pending wiki proposal for ``conv`` (the dispatch path)."""
		return api._propose_file_box_wiki_write(dict(args or _ARGS), conv)["approval"]

	# --- the hold ---------------------------------------------------------- #

	def test_propose_creates_pending_holding_the_payload(self):
		conv = self._conv()
		name = self._propose(conv)
		ar = frappe.get_doc(APPROVAL, name)
		self.assertEqual(ar.status, "Pending")
		self.assertEqual(ar.source, "File Box Wiki")
		self.assertEqual(ar.apply_status, "Pending")
		self.assertEqual(ar.owner, DROPPER)
		self.assertEqual(frappe.parse_json(ar.wiki_payload)["slug"], "party-fake-co")

	def test_propose_refuses_slugless(self):
		# F4: a slugless update_wiki is degenerate (the funnel refuses it too);
		# refuse at propose so slugless writes don't collapse onto one ref_name="".
		conv = self._conv()
		res = api._propose_file_box_wiki_write({"title": "no slug", "append_md": "x"}, conv)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.db.count(APPROVAL, {"conversation": conv, "source": "File Box Wiki"}), 0)

	def test_dedupe_refresh_leaves_a_decided_row_untouched(self):
		# F2: a re-emitted write must never rewrite a proposal a reviewer already
		# decided. Approve the row, then re-propose the same slug with a NEW payload:
		# the decided row is untouched and a FRESH proposal is created instead.
		conv = self._conv()
		first = self._propose(conv)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(first)
		second = api._propose_file_box_wiki_write({**_ARGS, "append_md": "## TAMPERED"}, conv)["approval"]
		self.assertNotEqual(second, first)
		# The approved row still holds the payload the reviewer saw.
		self.assertIn("INV-1", frappe.db.get_value(APPROVAL, first, "wiki_payload"))
		self.assertNotIn("TAMPERED", frappe.db.get_value(APPROVAL, first, "wiki_payload"))

	# --- approve ----------------------------------------------------------- #

	def test_reviewer_approve_lands_write_under_dropper_provenance(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel() as (apply, board), _as(REVIEWER):
			res = approvals_api.approve_wiki_write(name)
		self.assertTrue(res["applied"])
		# The funnel replayed once, keyed on the DROPPER's namespace (not REVIEWER).
		apply.assert_called_once()
		self.assertEqual(apply.call_args.kwargs["user"], DROPPER)
		self.assertEqual(board.call_args.kwargs["actor"], DROPPER)
		self.assertEqual(board.call_args.kwargs["provenance"], "reviewer_approved")
		ar = frappe.get_doc(APPROVAL, name)
		self.assertEqual(ar.status, "Approved")
		self.assertEqual(ar.apply_status, "Applied")
		self.assertEqual(ar.decided_by, REVIEWER)

	def test_non_reviewer_cannot_approve(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel() as (apply, _), _as(PLAIN):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.approve_wiki_write(name)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")

	def test_dropper_with_a_reviewer_role_approves_their_own_note(self):
		# The reviewer role is the gate, however many other reviewers the tenant has.
		name = self._propose(self._conv())
		with _funnel() as (apply, _), _as(DROPPER):
			res = approvals_api.approve_wiki_write(name)
		self.assertTrue(res["applied"])
		apply.assert_called_once()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Approved")

	def test_dropper_without_a_reviewer_role_cannot_approve_their_own_note(self):
		name = self._propose(self._conv(owner=PLAIN))
		with _funnel() as (apply, _), _as(PLAIN):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.approve_wiki_write(name)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")

	def test_double_approve_is_race_safe(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
			# The row is no longer Pending; a second approve refuses.
			with self.assertRaises(frappe.ValidationError):
				approvals_api.approve_wiki_write(name)

	def test_apply_failure_records_failed_status_and_reason(self):
		conv = self._conv()
		name = self._propose(conv)
		refused = [{"slug": "party-fake-co", "ok": False, "reason": "curated-collision refused"}]
		with _funnel(result=refused), _as(REVIEWER):
			res = approvals_api.approve_wiki_write(name)
		self.assertFalse(res["applied"])
		ar = frappe.get_doc(APPROVAL, name)
		# The decision still stands (Approved) but the write did not land.
		self.assertEqual(ar.status, "Approved")
		self.assertEqual(ar.apply_status, "Failed")
		self.assertIn("curated-collision", ar.apply_reason)

	def test_retry_re_drives_a_failed_landing(self):
		# The reconciliation path: a first approve whose write refused leaves the row
		# Approved + Failed; a later retry (funnel now healthy) lands it.
		conv = self._conv()
		name = self._propose(conv)
		refused = [{"slug": "party-fake-co", "ok": False, "reason": "deadlock"}]
		with _funnel(result=refused), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "apply_status"), "Failed")
		with _funnel() as (apply, _), _as(REVIEWER):
			res = approvals_api.retry_wiki_write(name)
		self.assertTrue(res["applied"])
		apply.assert_called_once()
		self.assertEqual(apply.call_args.kwargs["user"], DROPPER)
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "apply_status"), "Applied")

	def test_retry_refuses_already_applied(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		with _funnel() as (apply, _), _as(REVIEWER):
			with self.assertRaises(frappe.ValidationError):
				approvals_api.retry_wiki_write(name)
		apply.assert_not_called()

	def test_retry_refuses_pending_not_yet_approved(self):
		conv = self._conv()
		name = self._propose(conv)
		with _as(REVIEWER), self.assertRaises(frappe.ValidationError):
			approvals_api.retry_wiki_write(name)

	def test_non_reviewer_cannot_retry(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		with _as(PLAIN), self.assertRaises(frappe.PermissionError):
			approvals_api.retry_wiki_write(name)

	# --- reject ------------------------------------------------------------ #

	def test_reviewer_reject_writes_nothing(self):
		conv = self._conv()
		name = self._propose(conv)
		with _funnel() as (apply, _), _as(REVIEWER):
			res = approvals_api.reject_wiki_write(name)
		self.assertEqual(res["status"], "Rejected")
		apply.assert_not_called()
		ar = frappe.get_doc(APPROVAL, name)
		self.assertEqual(ar.status, "Rejected")
		self.assertEqual(ar.apply_status, "Pending")  # never applied
		self.assertEqual(ar.decided_by, REVIEWER)

	def test_non_reviewer_cannot_reject(self):
		conv = self._conv()
		name = self._propose(conv)
		with _as(PLAIN), self.assertRaises(frappe.PermissionError):
			approvals_api.reject_wiki_write(name)

	# --- reviewer lane read ------------------------------------------------ #

	def test_reviewer_lists_proposal_with_preview(self):
		conv = self._conv()
		name = self._propose(conv)
		with _as(REVIEWER):
			out = approvals_api.list_wiki_write_proposals()
		row = next(r for r in out["rows"] if r["name"] == name)
		self.assertEqual(row["preview"]["slug"], "party-fake-co")
		self.assertEqual(row["preview"]["append_md"], _ARGS["append_md"])
		self.assertEqual(row["dropper"], DROPPER)
		self.assertTrue(row["can_approve"])  # reviewer != dropper

	def test_list_orders_oldest_first_on_request(self):
		"""The board leads with the notes that waited longest, so it asks for them
		first; the default stays newest first."""
		old = self._propose(self._conv())
		new = self._propose(self._conv())
		frappe.db.set_value(APPROVAL, old, "creation", "2000-01-01 00:00:00", update_modified=False)
		frappe.db.set_value(APPROVAL, new, "creation", "2099-01-01 00:00:00", update_modified=False)
		frappe.db.commit()
		with _as(REVIEWER):
			newest = approvals_api.list_wiki_write_proposals()["rows"]
			oldest = approvals_api.list_wiki_write_proposals(order="oldest")["rows"]
			self.assertEqual((newest[0]["name"], oldest[0]["name"]), (new, old))
			with self.assertRaises(frappe.ValidationError):
				approvals_api.list_wiki_write_proposals(order="sideways")

	def test_non_reviewer_cannot_list_proposals(self):
		with _as(PLAIN), self.assertRaises(frappe.PermissionError):
			approvals_api.list_wiki_write_proposals()

	def test_actionable_list_surfaces_unlanded_approved_for_retry(self):
		# An approved write that FAILED to land stays actionable (needs_retry) so the
		# reviewer can re-drive it - it is not a dead-end Approved row.
		conv = self._conv()
		name = self._propose(conv)
		refused = [{"slug": "party-fake-co", "ok": False, "reason": "deadlock"}]
		with _funnel(result=refused), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		with _as(REVIEWER):
			out = approvals_api.list_wiki_write_proposals()  # default = Actionable
		row = next(r for r in out["rows"] if r["name"] == name)
		self.assertTrue(row["needs_retry"])
		self.assertFalse(row["can_approve"])
		self.assertEqual(row["apply_status"], "Failed")
		# Once it lands, it drops off the actionable list.
		with _funnel(), _as(REVIEWER):
			approvals_api.retry_wiki_write(name)
		with _as(REVIEWER):
			out2 = approvals_api.list_wiki_write_proposals()
		self.assertNotIn(name, {r["name"] for r in out2["rows"]})

	def test_list_offers_approve_to_a_dropper_with_a_reviewer_role(self):
		name = self._propose(self._conv())
		with _as(DROPPER):
			out = approvals_api.list_wiki_write_proposals()
		row = next(r for r in out["rows"] if r["name"] == name)
		self.assertTrue(row["can_approve"])
		# The full proposed body is carried for the reviewer to read before approving.
		self.assertEqual(row["preview"]["append_md"], _ARGS["append_md"])

	def test_land_and_record_refuses_a_second_landing(self):
		# F1 guard: once a row is Applied, _land_and_record (the shared executor for
		# approve + retry) must NOT run the funnel again - the landing-claim finds
		# nothing re-drivable and returns without a second append to the wiki.
		conv = self._conv()
		name = self._propose(conv)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		doc = frappe.get_doc(APPROVAL, name)
		self.assertEqual(doc.apply_status, "Applied")
		with _funnel() as (apply, _):
			res = approvals_api._land_and_record(name, doc, DROPPER)
		apply.assert_not_called()
		self.assertTrue(res["applied"])  # reports the existing landed state
		self.assertEqual(res["apply_status"], "Applied")

	def test_funnel_raise_leaves_row_redrivable_not_stuck(self):
		# A funnel that RAISES (txn-fatal deadlock) must never leave the row stuck in
		# the transient 'Applying' claim or falsely 'Applied' - it stays re-drivable.
		# rollback is mocked to keep the test's own transaction intact (the idiom
		# test_executor_txn_fatal_is_best_effort uses).
		conv = self._conv()
		name = self._propose(conv)
		with (
			_funnel(side_effect=Exception("deadlock")) as (apply, _),
			patch("frappe.db.rollback"),
			_as(REVIEWER),
		):
			res = approvals_api.approve_wiki_write(name)
		apply.assert_called_once()
		self.assertFalse(res["applied"])
		ar = frappe.get_doc(APPROVAL, name)
		self.assertEqual(ar.status, "Approved")
		self.assertNotIn(ar.apply_status, ("Applying", "Applied"))  # re-drivable, not stuck
		# It re-surfaces on the actionable list for a Retry.
		with _as(REVIEWER):
			out = approvals_api.list_wiki_write_proposals()
		self.assertIn(name, {r["name"] for r in out["rows"]})

	# --- customer-board guards (defense in depth) -------------------------- #

	def test_decide_refuses_wiki_write_row(self):
		conv = self._conv()
		name = self._propose(conv)
		with _as(DROPPER), self.assertRaises(frappe.PermissionError):
			approvals_api.decide(name, "yes", approve=1)
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")

	def test_dismiss_refuses_wiki_write_row(self):
		conv = self._conv()
		name = self._propose(conv)
		with _as(DROPPER), self.assertRaises(frappe.PermissionError):
			approvals_api.dismiss_approval(name)

	def test_wiki_rows_excluded_from_customer_board(self):
		conv = self._conv()
		wiki = self._propose(conv)
		# A normal File Box classification row on the same conversation still shows.
		normal = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "classify me",
				"status": "Pending",
				"source": "File Box",
				"conversation": conv,
				"question": "what is this?",
			}
		)
		normal.insert(ignore_permissions=True)
		frappe.db.set_value(APPROVAL, normal.name, "owner", DROPPER, update_modified=False)
		frappe.db.commit()
		with _as(DROPPER):
			page = approvals_api.list_approvals_page()
			count = approvals_api.pending_count()
		names = {r["name"] for r in page["rows"]}
		self.assertIn(normal.name, names)
		self.assertNotIn(wiki, names)
		# pending_count likewise ignores the wiki proposal.
		self.assertGreaterEqual(count, 1)
		with _as(DROPPER):
			# The wiki row is not in the count: dropping it and re-counting is stable.
			frappe.db.set_value(APPROVAL, wiki, "status", "Rejected", update_modified=False)
			self.assertEqual(approvals_api.pending_count(), count)


class TestWikiSourceConstantParity(FrappeTestCase):
	def test_source_constant_matches_across_modules(self):
		# The value is duplicated (api owns the propose path, approvals_api the
		# review lane) to avoid a heavy import; they must not drift.
		self.assertEqual(api.FILE_BOX_WIKI_SOURCE, approvals_api.FILE_BOX_WIKI_SOURCE)
		self.assertEqual(api.FILE_BOX_WIKI_SOURCE, "File Box Wiki")


class _WikiBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(DROPPER, ("Jarvis User", "Jarvis Admin"))
		_ensure_user(REVIEWER, ("Jarvis Admin",))

	def setUp(self):
		super().setUp()
		self.addCleanup(self._wipe)
		self.conv = self._conv()

	def _wipe(self):
		for name in frappe.get_all(CONV, filters={"title": "wiki-review test"}, pluck="name"):
			frappe.db.delete(APPROVAL, {"conversation": name})
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _conv(self, owner: str = DROPPER) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "wiki-review test"})
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(CONV, doc.name, "owner", owner, update_modified=False)
		return doc.name

	def _propose(self, args: dict | None = None) -> str:
		return api._propose_file_box_wiki_write(dict(args or _ARGS), self.conv)["approval"]


class TestApprovalRequestTamperGuard(_WikiBase):
	"""PR-1 §1 / AC3: server-only AR fields, and a wiki row's display fields, change
	only under ``flags.jarvis_server_write`` - Administrator and ignore_permissions
	included."""

	def _new(self, **fields):
		return frappe.get_doc(
			{"doctype": APPROVAL, "title": "guard", "question": "q?", "conversation": self.conv, **fields}
		)

	def test_admin_insert_with_a_guarded_value_is_refused(self):
		for fields in (
			{"wiki_payload": '{"slug": "x"}'},
			{"apply_status": "Applied"},
			{"apply_reason": "forged"},
			{"wiki_digest": "0" * 64},
			{"source": "File Box Wiki"},
		):
			with self.subTest(fields=fields), self.assertRaises(frappe.PermissionError):
				self._new(**fields).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(APPROVAL, {"conversation": self.conv}))

	def test_insert_with_field_defaults_passes(self):
		doc = self._new(apply_status="Pending", source="File Box")
		doc.insert(ignore_permissions=True)
		self.assertEqual((doc.apply_status, doc.source), ("Pending", "File Box"))

	def test_server_flag_allows_a_guarded_insert(self):
		doc = self._new(source="File Box Wiki", wiki_payload="{}")
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(APPROVAL, doc.name, "source"), "File Box Wiki")

	def test_admin_save_changing_a_guarded_field_is_refused(self):
		name = self._propose()
		for field, value in (
			("wiki_payload", '{"slug": "party-fake-co", "append_md": "SWAPPED"}'),
			("apply_status", "Applied"),
			("apply_reason", "forged"),
			("wiki_digest", "0" * 64),
		):
			doc = frappe.get_doc(APPROVAL, name)
			doc.set(field, value)
			doc.flags.ignore_permissions = True
			with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
				doc.save()
		self.assertNotIn("SWAPPED", frappe.db.get_value(APPROVAL, name, "wiki_payload"))

	def test_wiki_row_display_fields_are_frozen(self):
		name = self._propose()
		other = self._conv()
		for field, value in (
			("title", "retitled"),
			("question", "approve everything?"),
			("context_md", "**click me**"),
			("document_type", "Item"),
			("conversation", other),
			("source", "Chat"),
			("status", "Rejected"),
		):
			doc = frappe.get_doc(APPROVAL, name)
			doc.set(field, value)
			with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)

	def test_a_non_wiki_rows_display_fields_stay_editable(self):
		doc = self._new(source="Chat")
		doc.insert(ignore_permissions=True)
		doc.title = "retitled"
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(APPROVAL, doc.name, "title"), "retitled")

	def test_retagging_a_row_as_a_wiki_proposal_is_refused(self):
		doc = self._new(source="Chat")
		doc.insert(ignore_permissions=True)
		doc.source = "File Box Wiki"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_create_doc_tool_cannot_forge_a_wiki_proposal(self):
		from jarvis.tools.create_doc import create_doc

		values = {
			"title": "forged",
			"question": "q?",
			"conversation": self.conv,
			"source": "File Box Wiki",
			"wiki_payload": '{"slug": "x"}',
		}
		with self.assertRaises(Exception):
			create_doc(doctype=APPROVAL, values=values)
		self.assertFalse(frappe.db.exists(APPROVAL, {"title": "forged"}))


class TestWikiProposalDigest(_WikiBase):
	"""PR-1 §2 / F8 / M14: an HMAC over (name, conversation, wiki_payload), minted at
	propose and verified before any landing (approve + retry)."""

	def _row(self, name):
		return frappe.db.get_value(
			APPROVAL, name, ["conversation", "wiki_payload", "wiki_digest"], as_dict=True
		)

	def _ok(self, name) -> bool:
		r = self._row(name)
		return approvals_api.wiki_digest_ok(name, r.conversation, r.wiki_payload, r.wiki_digest)

	def test_propose_mints_a_valid_digest(self):
		name = self._propose()
		self.assertEqual(len(self._row(name).wiki_digest), 64)
		self.assertTrue(self._ok(name))

	def test_a_payload_with_markup_verifies_after_the_insert(self):
		# The ORM insert sanitizes HTML-looking text; the digest covers what is STORED.
		name = self._propose({**_ARGS, "append_md": "## Invoice <b>INV-1</b> <script>x</script>"})
		self.assertTrue(self._ok(name))
		with _funnel() as (apply, _), _as(REVIEWER):
			self.assertTrue(approvals_api.approve_wiki_write(name)["applied"])
		apply.assert_called_once()

	def test_refresh_re_mints_in_the_same_update(self):
		name = self._propose()
		before = self._row(name).wiki_digest
		again = self._propose({**_ARGS, "append_md": "## Invoice INV-2"})
		self.assertEqual(again, name)
		self.assertNotEqual(self._row(name).wiki_digest, before)
		self.assertTrue(self._ok(name))

	def test_digest_binds_name_conversation_and_payload(self):
		name = self._propose()
		r = self._row(name)
		self.assertFalse(approvals_api.wiki_digest_ok("other", r.conversation, r.wiki_payload, r.wiki_digest))
		self.assertFalse(approvals_api.wiki_digest_ok(name, "other", r.wiki_payload, r.wiki_digest))
		self.assertFalse(
			approvals_api.wiki_digest_ok(name, r.conversation, r.wiki_payload + " ", r.wiki_digest)
		)
		self.assertFalse(approvals_api.wiki_digest_ok(name, r.conversation, r.wiki_payload, None))

	def _refused(self, name, **kw):
		with _funnel() as (apply, _), _as(REVIEWER), self.assertRaises(frappe.PermissionError):
			approvals_api.approve_wiki_write(name, **kw)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")

	def test_a_swapped_payload_refuses_approve(self):
		name = self._propose()
		# A raw write (a pre-guard forge, or SQL-level tamper) past the ORM guard.
		frappe.db.set_value(APPROVAL, name, "wiki_payload", '{"slug": "party-fake-co", "append_md": "EVIL"}')
		self._refused(name)

	def test_a_tampered_or_missing_digest_refuses_approve(self):
		name = self._propose()
		frappe.db.set_value(APPROVAL, name, "wiki_digest", "0" * 64)
		self._refused(name)
		frappe.db.set_value(APPROVAL, name, "wiki_digest", None)
		self._refused(name)

	def test_a_transplanted_row_refuses_approve(self):
		name = self._propose()
		frappe.db.set_value(APPROVAL, name, "conversation", self._conv())
		self._refused(name)

	def test_expected_digest_must_match_the_reviewed_payload(self):
		name = self._propose()
		self._refused(name, expected_digest="0" * 64)
		with _funnel() as (apply, _), _as(REVIEWER):
			res = approvals_api.approve_wiki_write(name, expected_digest=self._row(name).wiki_digest)
		self.assertTrue(res["applied"])
		apply.assert_called_once()

	def test_a_refresh_between_the_check_and_the_claim_is_refused(self):
		# The claim is pinned to the verified digest; a refresh re-mints it.
		name = self._propose()
		digest = self._row(name).wiki_digest

		def refresh(_doc):
			self._propose({**_ARGS, "append_md": "## Invoice INV-2"})
			return DROPPER

		with (
			_funnel() as (apply, _),
			_as(REVIEWER),
			patch.object(approvals_api, "_dropper_of", side_effect=refresh),
			self.assertRaisesRegex(frappe.PermissionError, "changed since you opened it"),
		):
			approvals_api.approve_wiki_write(name, expected_digest=digest)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")
		self.assertNotEqual(self._row(name).wiki_digest, digest)

	def test_retry_verifies_the_digest(self):
		name = self._propose()
		refused = [{"slug": "party-fake-co", "ok": False, "reason": "deadlock"}]
		with _funnel(result=refused), _as(REVIEWER):
			approvals_api.approve_wiki_write(name)
		frappe.db.set_value(APPROVAL, name, "wiki_payload", '{"slug": "party-fake-co", "append_md": "EVIL"}')
		with _funnel() as (apply, _), _as(REVIEWER), self.assertRaises(frappe.PermissionError):
			approvals_api.retry_wiki_write(name)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "apply_status"), "Failed")

	def test_land_and_record_verifies_under_the_claim(self):
		# The executor re-reads + verifies the row it claimed, so a swap after the
		# endpoint's own check (TOCTOU) never lands.
		name = self._propose()
		doc = frappe.get_doc(APPROVAL, name)
		frappe.db.set_value(APPROVAL, name, "wiki_payload", '{"slug": "party-fake-co", "append_md": "EVIL"}')
		with _funnel() as (apply, _), self.assertRaises(frappe.PermissionError):
			approvals_api._land_and_record(name, doc, DROPPER)
		apply.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "apply_status"), "Pending")

	def test_reviewer_list_carries_the_digest(self):
		name = self._propose()
		with _as(REVIEWER):
			rows = approvals_api.list_wiki_write_proposals()["rows"]
		row = next(r for r in rows if r["name"] == name)
		self.assertEqual(row["wiki_digest"], self._row(name).wiki_digest)

	def test_patch_mints_for_pending_and_unlanded_rows_only(self):
		from jarvis.patches import v2_22_mint_wiki_proposal_digests as patch_mod

		pending = self._propose()
		unlanded = self._propose({**_ARGS, "slug": "party-two"})
		landed = self._propose({**_ARGS, "slug": "party-three"})
		refused = [{"slug": "party-two", "ok": False, "reason": "deadlock"}]
		with _funnel(result=refused), _as(REVIEWER):
			approvals_api.approve_wiki_write(unlanded)
		with _funnel(), _as(REVIEWER):
			approvals_api.approve_wiki_write(landed)
		for n in (pending, unlanded, landed):
			frappe.db.set_value(APPROVAL, n, "wiki_digest", None)
		patch_mod.execute()
		self.assertTrue(self._ok(pending))
		self.assertTrue(self._ok(unlanded))
		self.assertIsNone(self._row(landed).wiki_digest)
		minted = self._row(pending).wiki_digest
		patch_mod.execute()  # idempotent
		self.assertEqual(self._row(pending).wiki_digest, minted)
