"""Jarvis Turn Effect DocType controller.

Server-owned append-only enrichment idempotency ledger (D2 §1a, OAR-9). One row
per ``(turn, effect_name)``; the composite doc name (``format:{turn}::{effect_name}``)
IS the UNIQUE key, so a duplicate finalize collides on primary-key insert and
no-ops. Every mutation goes through a server path with ``ignore_permissions=True``;
there is no end-user create/write/delete. All lifecycle logic lives in
``jarvis.chat.turn_state`` (insert-required / claim / complete / force-done), not
in ORM validation — the finalize path never uses ``save()``.
"""

import frappe
from frappe.model.document import Document


class JarvisTurnEffect(Document):
	# Minimal controller. The composite name (turn::effect_name) enforces the
	# UNIQUE (turn, effect_name) idempotency key at the PK level.
	pass
