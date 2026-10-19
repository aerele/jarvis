"""Real, transaction-local fixtures for the sharing security boundary."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.permissions import ensure_jarvis_admin_role, ensure_jarvis_user_role
from jarvis.tests._conv_helpers import _make_conv

OWNER = "share-owner@example.com"
RECIPIENT = "share-recipient@example.com"
OUTSIDER = "share-outsider@example.com"
DISABLED = "share-disabled@example.com"
WEBSITE = "share-website@example.com"
MANAGER = "share-manager@example.com"
ADMIN = "share-admin@example.com"
NO_ACCESS = "share-no-access@example.com"
SHARE = "Jarvis Conversation Share"
MESSAGE = "Jarvis Chat Message"


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def make_user(email, role="Jarvis User", *, enabled=1, user_type="System User"):
	with as_user("Administrator"):
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"enabled": enabled,
					"user_type": user_type,
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			if role:
				user.add_roles(role)
	return email


def make_people():
	ensure_jarvis_user_role()
	ensure_jarvis_admin_role()
	for email, role, enabled, kind in (
		(OWNER, "Jarvis User", 1, "System User"),
		(RECIPIENT, "Jarvis User", 1, "System User"),
		(OUTSIDER, "Jarvis User", 1, "System User"),
		(DISABLED, "Jarvis User", 0, "System User"),
		(WEBSITE, None, 1, "Website User"),
		(MANAGER, "System Manager", 1, "System User"),
		(ADMIN, "Jarvis Admin", 1, "System User"),
		(NO_ACCESS, None, 1, "System User"),
	):
		make_user(email, role, enabled=enabled, user_type=kind)


def make_message(conversation, *, role="user", **values):
	doc = frappe.get_doc(
		{
			"doctype": MESSAGE,
			"conversation": conversation,
			"seq": (
				frappe.db.get_value(MESSAGE, {"conversation": conversation}, "seq", order_by="seq desc") or 0
			)
			+ 1,
			"role": role,
			"content": "Shared transcript sentinel",
			**values,
		}
	)
	doc.flags.jarvis_server_write = True
	doc.insert(ignore_permissions=True)
	return doc.name


def make_chat(owner=OWNER, **values):
	# Reuse the established fixture without allowing its historical explicit
	# commit to escape this suite's savepoint. Product commits are not patched.
	with patch.object(frappe.db, "commit"):
		conversation = _make_conv(owner)
	if values:
		frappe.db.set_value("Jarvis Conversation", conversation, values, update_modified=False)
	with as_user(owner):
		message = make_message(conversation)
	return conversation, message


def add_share(conversation, user=RECIPIENT):
	from jarvis.chat.sharing import set_shares

	with as_user(OWNER):
		return set_shares(conversation, [user])


def payload(value):
	return json.dumps(value, sort_keys=True, default=str)
