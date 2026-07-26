"""Jarvis Shared Skill Slug DocType controller (SR4-2).

A DB-unique reservation of a shared-skill slug. The doctype ``name`` IS the bare
authored slug (``autoname: field:slug``), so the primary-key constraint makes it
impossible for two SHARED (Role/Org) Jarvis Custom Skills to carry the same slug
regardless of the write path or concurrency - the second insert fails on the PK.
This is the hard guarantee under the controller uniqueness belt
(``JarvisCustomSkill._validate_shared_slug_unique``): the belt's SELECT catches the
sequential case fast, and this row catches two creators that both pass that SELECT
before either commits.

Rows are managed entirely by the ``reserve_shared_slug`` / ``release_shared_slug``
helpers in ``jarvis.chat.custom_skills``, driven from the skill controller's
on_update / on_trash hooks; there is no user-facing surface.
"""

from frappe.model.document import Document


class JarvisSharedSkillSlug(Document):
	pass
