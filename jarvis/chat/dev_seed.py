"""Dev-only seeder for the four feature pages (Skills / Macros / File Box /
Approvals) at the scale the migration design targets (§7.1 of
chat-features-page-migration-design.md).

    bench --site jarvis.localhost execute \\
        jarvis.chat.dev_seed.seed_feature_pages \\
        --kwargs "{'user':'vignesh@aerele.in'}"

Guarded by ``frappe.conf.developer_mode`` so it can never run on a real site.
It seeds a SECOND user's data too, so owner-scoping can be proven (their rows
must never appear in the primary user's pages). Re-running wipes the previous
seed for both users first, so it is idempotent.

NOTE: rows are inserted with ``flags.ignore_validate`` to bypass the per-owner
skill cap (25) and slug/uniqueness checks — this is seed data, not user input.
"""

from __future__ import annotations

import random

import frappe
from frappe.utils import add_days, now_datetime

from jarvis.permissions import require_jarvis_user

SECOND_USER = "seed-userb@example.com"

_SKILL = "Jarvis Custom Skill"
_MACRO = "Jarvis Macro"
_CONV = "Jarvis Conversation"
_MSG = "Jarvis Chat Message"
_APPROVAL = "Jarvis Approval Request"

_DESC_WORDS = [
	"invoice",
	"reconciliation",
	"payroll",
	"audit",
	"expense",
	"vendor",
	"purchase order",
	"sales",
	"journal",
	"inventory",
	"tax",
	"compliance",
	"onboarding",
	"reminder",
	"summary",
	"forecast",
	"budget",
	"ledger",
]
_DOC_TYPES = [
	"Purchase Invoice",
	"Sales Invoice",
	"Payment Entry",
	"Journal Entry",
	"Expense Claim",
	"Purchase Order",
	"",  # "" -> Unclassified
]
_FREQS = ["daily", "weekly", "monthly"]

# Genuinely varied 3+ option payloads for currently-PENDING approvals, so the
# varied-chip decide UI is exercisable live (not just Approve/Reject pairs).
_VARIED_PENDING = [
	{
		"title": "GST rate mismatch on inbound vendor invoice",
		"document_type": "Purchase Invoice",
		"question": "Line 4 is billed at 12% GST but the HSN code maps to 18%. How should it be posted?",
		"context_md": "Vendor billed 12% on HSN 3304; the item master maps that HSN to 18%.",
		"options": ["Post as drafted", "Post with 18% GST correction", "Hold for vendor confirmation"],
	},
	{
		"title": "Duplicate-looking supplier bill",
		"document_type": "Purchase Invoice",
		"question": "Bill INV-2214 matches INV-2209 on amount and date. Proceed?",
		"context_md": "Same supplier, same 41,300.00 total, dated one day apart.",
		"options": [
			"Post anyway (distinct delivery)",
			"Merge into INV-2209",
			"Reject as duplicate",
			"Ask supplier for clarification",
		],
	},
	{
		"title": "Freight charge without a purchase order",
		"document_type": "Journal Entry",
		"question": "A 7,850.00 freight charge has no matching PO. Where should it be booked?",
		"context_md": "Carrier invoice references a delivery challan, not a PO.",
		"options": [
			"Book under Freight expense",
			"Book to the project cost center",
			"Park in suspense until PO is raised",
		],
	},
	{
		"title": "Customer overpayment on SINV-0092",
		"document_type": "Payment Entry",
		"question": "Payment received exceeds the invoice by 2,000.00. How should the excess be handled?",
		"context_md": "Bank credit of 52,000.00 against a 50,000.00 sales invoice.",
		"options": ["Allocate to the next open invoice", "Record as advance", "Refund the excess"],
	},
	{
		"title": "Expense claim missing one receipt",
		"document_type": "Expense Claim",
		"question": "Claim EC-118 has 5 lines but only 4 receipts. Approve?",
		"context_md": "Missing receipt is a 640.00 taxi fare; claimant says it was a cash ride.",
		"options": [
			"Approve in full",
			"Approve excluding the unreceipted line",
			"Return to claimant for the receipt",
		],
	},
	{
		"title": "Unreadable scan — document class unclear",
		"document_type": "",
		"question": "The dropped file is a low-quality scan; it could be a quotation or a proforma invoice. How to proceed?",
		"context_md": "OCR confidence below threshold on the header block.",
		"options": [
			"Treat as quotation",
			"Treat as proforma invoice",
			"Request a re-scan",
			"Discard the file",
		],
	},
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_dev() -> None:
	if not frappe.conf.developer_mode:
		frappe.throw("dev_seed is only allowed with developer_mode enabled.")


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
	return email


def _insert(doc_dict: dict) -> "frappe.model.document.Document":
	"""Insert bypassing controller validate (seed data), owner = session user."""
	doc = frappe.get_doc(doc_dict)
	doc.flags.ignore_validate = True
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _set_creation(doctype: str, name: str, days_ago: int) -> None:
	when = add_days(now_datetime(), -days_ago)
	frappe.db.set_value(doctype, name, "creation", when, update_modified=False)


def _wipe(user: str) -> None:
	"""Remove a prior seed run for ``user`` (by the seed's name/title markers)."""
	for name in frappe.get_all(
		_SKILL, filters={"owner": user, "skill_name": ["like", "seed-%"]}, pluck="name"
	):
		frappe.delete_doc(_SKILL, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(
		_MACRO, filters={"owner": user, "macro_name": ["like", "Seed %"]}, pluck="name"
	):
		for run in frappe.get_all("Jarvis Macro Run", filters={"macro": name}, pluck="name"):
			frappe.delete_doc("Jarvis Macro Run", run, ignore_permissions=True, force=True)
		frappe.delete_doc(_MACRO, name, ignore_permissions=True, force=True)
	convs = frappe.get_all(_CONV, filters={"owner": user, "title": ["like", "File: seed-%"]}, pluck="name")
	for conv in convs:
		for ap in frappe.get_all(_APPROVAL, filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc(_APPROVAL, ap, ignore_permissions=True, force=True)
		frappe.db.delete(_MSG, {"conversation": conv})
		frappe.delete_doc(_CONV, conv, ignore_permissions=True, force=True)
	frappe.db.commit()


# --------------------------------------------------------------------------- #
# per-feature seeders (run inside a set_user(owner) context)
# --------------------------------------------------------------------------- #
def _seed_skills(owner: str, n: int, prefix: str, share_to: str | None) -> None:
	for i in range(1, n + 1):
		enabled = 0 if i % 8 == 0 else 1  # ~1/8 drafts
		words = ", ".join(random.sample(_DESC_WORDS, 3))
		shared = []
		# ~1 in 12 own+enabled rows shared with the other user (1-3 dup rows collapse to 1 user)
		if share_to and enabled and i % 12 == 0:
			shared = [{"user": share_to}]
		_insert(
			{
				"doctype": _SKILL,
				"skill_name": f"{prefix}{i:03d}",
				"description": f"Seed skill {i}: handles {words}.",
				"instructions": f"# {prefix}{i:03d}\n\nDo the {words} work described above.\n",
				"user_invocable": 0 if i % 3 == 0 else 1,
				"enabled": enabled,
				"shared_with": shared,
			}
		)
		if i % 40 == 0:
			frappe.db.commit()
	frappe.db.commit()


def _seed_macros(owner: str, n: int, prefix: str) -> None:
	for i in range(1, n + 1):
		nsteps = 1 + (i % 6)
		steps = [
			{"label": f"step {s}", "prompt": f"Seed macro {i} step {s}: do the thing."}
			for s in range(1, nsteps + 1)
		]
		enabled = 0 if i % 9 == 0 else 1
		sched = i % 4 == 0  # ~30/120 scheduled
		merged, merge_status = "", ""
		if i % 6 == 0:  # ~20/120 have a ready summary
			merged, merge_status = f"Merged prompt for macro {i}.", "ready"
		elif i % 37 == 0:  # a few pending (gate the Run button)
			merge_status = "pending"
		doc = _insert(
			{
				"doctype": _MACRO,
				"macro_name": f"{prefix}{i:03d}",
				"description": f"Seed macro {i} ({nsteps} steps).",
				"enabled": enabled,
				"stop_on_error": 1 if i % 2 else 0,
				"schedule_enabled": 1 if sched else 0,
				"schedule_frequency": _FREQS[i % 3] if sched else "daily",
				"schedule_time": "09:00:00" if sched else None,
				"steps": steps,
				"merged_prompt": merged,
				"merge_status": merge_status,
			}
		)
		# spread last_run_at / next_run_at for sort coverage
		if i % 3 == 0:
			frappe.db.set_value(
				_MACRO, doc.name, "last_run_at", add_days(now_datetime(), -(i % 30)), update_modified=False
			)
		if sched:
			frappe.db.set_value(
				_MACRO, doc.name, "next_run_at", add_days(now_datetime(), 1 + (i % 7)), update_modified=False
			)
		if i % 40 == 0:
			frappe.db.commit()
	frappe.db.commit()


def _add_msg(conv: str, seq: int, role: str, content: str, streaming=0, error="") -> None:
	_insert(
		{
			"doctype": _MSG,
			"conversation": conv,
			"seq": seq,
			"role": role,
			"content": content,
			"streaming": streaming,
			"error": error,
		}
	)


def _seed_filebox(owner: str, n: int, prefix: str) -> list[str]:
	"""Seed n File-Box conversations with the §7.1 status mix. Returns their names."""
	names: list[str] = []
	for i in range(1, n + 1):
		doc = _insert(
			{
				"doctype": _CONV,
				"title": f"File: {prefix}{i:04d}.pdf",
				"file_box": 1,
				"status": "Active",
			}
		)
		_set_creation(_CONV, doc.name, days_ago=(i * 400) // max(n, 1))
		names.append(doc.name)
		_add_msg(doc.name, 1, "user", "process this inbound document")
		r = i % 100
		if r < 60:  # ~60% done
			_add_msg(doc.name, 2, "assistant", f"Drafted document {i}.", streaming=0)
		elif r < 75:  # ~15% error
			_add_msg(doc.name, 2, "assistant", "", streaming=0, error="Simulated OCR failure")
		elif r < 90:  # ~15% needs_approval
			_add_msg(doc.name, 2, "assistant", f"Queued {i} for approval.", streaming=0)
			for k in range(1, 1 + (i % 3) + 1):
				_insert(
					{
						"doctype": _APPROVAL,
						"title": f"Approve line {k} of doc {i}",
						"status": "Pending",
						"document_type": _DOC_TYPES[(i + k) % len(_DOC_TYPES)],
						"conversation": doc.name,
						"question": f"Confirm value on line {k}?",
						"context_md": f"Extracted context for doc {i} line {k}.",
						"options": '["Approve as drafted", "Reject"]',
					}
				)
		# else ~10% processing: no assistant message at all
		if i % 50 == 0:
			frappe.db.commit()
	frappe.db.commit()
	return names


def _seed_standalone_approvals(owner: str, conv_pool: list[str], n_pending: int, n_decided: int) -> None:
	pool = conv_pool or [None]
	for i in range(n_pending):
		_insert(
			{
				"doctype": _APPROVAL,
				"title": f"Standalone pending approval {i}",
				"status": "Pending",
				"document_type": _DOC_TYPES[i % len(_DOC_TYPES)],
				"conversation": pool[i % len(pool)],
				"question": f"Decide on standalone item {i}?",
				"context_md": f"Context for standalone item {i}.",
				"options": '["Approve", "Reject"]',
			}
		)
	for i in range(n_decided):
		status = "Approved" if i % 2 == 0 else "Rejected"
		doc = _insert(
			{
				"doctype": _APPROVAL,
				"title": f"Standalone decided approval {i}",
				"status": status,
				"document_type": _DOC_TYPES[i % len(_DOC_TYPES)],
				"conversation": pool[i % len(pool)],
				"question": f"Historical decision {i}?",
				"context_md": f"Context for decided item {i}.",
				"options": '["Approve", "Reject"]',
				"decision": "Approve as drafted" if status == "Approved" else "Reject",
				"decided_by": owner,
				"decided_at": now_datetime(),
			}
		)
		frappe.db.set_value(
			_APPROVAL, doc.name, "decided_at", add_days(now_datetime(), -(i % 20)), update_modified=False
		)
	frappe.db.commit()


def _wipe_varied(user: str) -> None:
	"""Remove a prior varied-approvals seed for ``user`` (by its markers)."""
	for name in frappe.get_all(
		_APPROVAL, filters={"owner": user, "title": ["like", "Seed varied: %"]}, pluck="name"
	):
		frappe.delete_doc(_APPROVAL, name, ignore_permissions=True, force=True)
	for conv in frappe.get_all(
		_CONV, filters={"owner": user, "title": "seed-varied approvals"}, pluck="name"
	):
		frappe.db.delete(_MSG, {"conversation": conv})
		frappe.delete_doc(_CONV, conv, ignore_permissions=True, force=True)
	frappe.db.commit()


@frappe.whitelist()
@require_jarvis_user
def seed_varied_approvals(user: str) -> dict:
	"""Seed ~6 currently-PENDING approvals with genuinely varied 3+ option
	payloads (see ``_VARIED_PENDING``) so the varied-chip decide UI is
	exercisable live. DEV ONLY (developer_mode guard) and idempotent — wipes its
	own previous rows by marker first, touching nothing else. Runnable standalone:

	    bench --site jarvis.localhost execute \\
	        jarvis.chat.dev_seed.seed_varied_approvals \\
	        --kwargs "{'user':'vignesh@aerele.in'}"
	"""
	_require_dev()
	if not frappe.db.exists("User", user):
		frappe.throw(f"Unknown user: {user}")
	_wipe_varied(user)
	original = frappe.session.user
	frappe.set_user(user)
	try:
		conv = _insert({"doctype": _CONV, "title": "seed-varied approvals", "status": "Active"})
		for spec in _VARIED_PENDING:
			_insert(
				{
					"doctype": _APPROVAL,
					"title": f"Seed varied: {spec['title']}",
					"status": "Pending",
					"document_type": spec["document_type"],
					"conversation": conv.name,
					"question": spec["question"],
					"context_md": spec["context_md"],
					"options": frappe.as_json(spec["options"]),
				}
			)
	finally:
		frappe.set_user(original)
	frappe.db.commit()
	return {"ok": True, "user": user, "varied_pending": len(_VARIED_PENDING)}


def _seed_for(owner: str, share_to: str | None, scale: str) -> None:
	"""Seed one user's data. ``scale`` = 'full' (primary) or 'small' (scoping proof)."""
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		if scale == "full":
			_seed_skills(owner, 120, "seed-skill-", share_to)
			_seed_macros(owner, 120, "Seed macro ")
			convs = _seed_filebox(owner, 1500, "seed-")
			_seed_standalone_approvals(owner, convs, n_pending=40, n_decided=20)
		else:
			_seed_skills(owner, 20, "seed-skill-", None)
			_seed_macros(owner, 20, "Seed macro ")
			convs = _seed_filebox(owner, 50, "seed-")
			_seed_standalone_approvals(owner, convs, n_pending=8, n_decided=4)
	finally:
		frappe.set_user(original)


@frappe.whitelist()
@require_jarvis_user
def seed_feature_pages(user: str) -> dict:
	"""Seed the four feature pages for ``user`` at scale, plus a small scoping
	dataset for a second user. DEV ONLY. Not called by tests (tests build their
	own fixtures)."""
	_require_dev()
	if not frappe.db.exists("User", user):
		frappe.throw(f"Unknown user: {user}")
	other = _ensure_user(SECOND_USER)

	_wipe(user)
	_wipe(other)

	# Primary user shares a few skills with the second user (proves the
	# shared-with-me visibility path in list_custom_skills_page).
	_seed_for(user, share_to=other, scale="full")
	# Second user's own data — must NEVER appear in the primary user's pages.
	_seed_for(other, share_to=None, scale="small")
	# Varied 3+ option pending approvals (exercises the varied-chip decide UI).
	seed_varied_approvals(user)

	frappe.db.commit()
	return {
		"ok": True,
		"user": user,
		"second_user": other,
		"seeded": {
			"skills": 120,
			"macros": 120,
			"filebox_conversations": 1500,
			"standalone_approvals": 60,
			"varied_pending": len(_VARIED_PENDING),
		},
	}
