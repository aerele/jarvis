"""Jarvis User Model Usage — child table of Jarvis User Settings.

One row per (user, model, month_key). Holds the model's month-to-date token
counters plus the admin-set per-model cap (monthly_token_limit, 0 = unlimited).
Rows are written ONLY by server code in jarvis.chat.usage via atomic SQL (never
a desk save), so this controller stays minimal.
"""

import frappe
from frappe.model.document import Document


class JarvisUserModelUsage(Document):
	# Identity is (parent, model, month_key). Counters + cap are mutated only by
	# jarvis.chat.usage; no validation hook needed.
	pass


def on_doctype_update():
	"""Create the (parent, parentfield, model, month_key) unique index.

	The v2_02 patch adds this index too, but a patch cannot be its only home:
	``install_app`` runs with ``set_as_patched=True``, which calls
	``set_all_patches_as_completed`` and marks every entry in patches.txt as
	executed WITHOUT running it. So on any fresh install -- CI, a new customer
	bench, a site rebuilt from scratch -- the patch is skipped and the index
	never exists, silently removing the guard that makes the duplicate-row race
	impossible. Frappe calls on_doctype_update() on every model sync, fresh
	installs included, so the constraint belongs here; the patch stays for
	existing DBs, where it also de-dupes the rows before adding the index.

	add_unique no-ops when the index is already present, so an upgraded site
	running both paths is harmless.
	"""
	frappe.db.add_unique(
		"Jarvis User Model Usage",
		["parent", "parentfield", "model", "month_key"],
		constraint_name="parent_field_model_month",
	)
