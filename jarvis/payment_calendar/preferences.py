"""Store selection identities only, under the existing user's settings owner gate."""

import json

import frappe

from jarvis.payment_calendar.access import refresh_actor_access
from jarvis.payment_calendar.scope import normalize_scope

FIELD = "payment_calendar_preferences"
DOCTYPE = "Jarvis User Settings"


def normalize_preferences(value):
	if value in (None, ""):
		return None
	if isinstance(value, str):
		if len(value) > 8192:
			raise frappe.ValidationError("Choose a company and up to 50 accounts.")
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise frappe.ValidationError("Choose a company and bank accounts.") from exc
	if not isinstance(value, dict) or set(value) != {"company", "accounts"}:
		raise frappe.ValidationError("Only company and account selections can be saved.")
	scope = normalize_scope(value["company"], value["accounts"])
	return {key: scope[key] for key in ("company", "accounts")}


def validate_document(doc):
	if not doc.has_value_changed(FIELD):
		return
	if doc.get(FIELD) and doc.user != frappe.session.user:
		raise frappe.PermissionError("Save your own Payment Calendar preferences.")
	value = normalize_preferences(doc.get(FIELD))
	doc.set(FIELD, json.dumps(value, separators=(",", ":")) if value else "")


def get_preferences() -> dict | None:
	actor = refresh_actor_access()
	value = frappe.db.get_value(DOCTYPE, {"user": actor}, FIELD)
	try:
		return normalize_preferences(value)
	except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError):
		return None


def save_preferences(company: str, accounts: list[str] | str | None) -> dict:
	from jarvis.chat.usage import get_or_create_user_settings

	actor = refresh_actor_access()
	value = normalize_preferences({"company": company, "accounts": accounts})
	doc = get_or_create_user_settings(actor)
	doc.set(FIELD, json.dumps(value, separators=(",", ":")))
	doc.save()
	return value
