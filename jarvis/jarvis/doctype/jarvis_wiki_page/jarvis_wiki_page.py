"""Jarvis Wiki Page DocType controller.

One page of durable org knowledge (a customer, supplier, item, process,
transactional-doctype convention, exception, integration, person or org-wide
note), keyed by a lowercase ``slug`` (``autoname: field:slug``). Pages are
written by the wiki ingest / voice-facts extraction
(``jarvis.chat.wiki.apply_extracted_page_updates``), the ``update_wiki`` agent
tool and the SPA's ``save_wiki_page`` endpoint; all of those funnel through
this controller, so the slug/length invariants hold no matter who wrote.

Slug grammar: alnum runs separated by single hyphens, with ``--`` reserved as
the type separator (``customer--acme-corp``, ``doctype--sales-invoice``).

Scopes (wiki v2): ``Org`` (default; every desk user), ``Role`` (holders of
``target_role``), ``User`` (``target_user`` only). Non-Org slugs get an
audience suffix derived at create time (``--u-<localpart>`` / ``--r-<role>``)
so the same base slug can exist per user/role without colliding on the
docname. Visibility is enforced in ``jarvis.chat.wiki_permissions``; this
controller owns scope consistency and the SM-only scope-change guard.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.learning.sanitizer import (
	SANITIZED_PLACEHOLDER,
	scan_instruction_injection,
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-{1,2}[a-z0-9]+)*$")
MAX_SLUG_LEN = 140
MAX_SUMMARY_LEN = 500
MAX_BODY_LEN = 20000

SCOPES = ("Org", "Role", "User")

# Cached "org has >=1 Active wiki page" flag consumed by the per-turn
# wiki_clause hot path; invalidated by every controller write below.
WIKI_HAS_PAGES_CACHE_KEY = "jarvis:wiki_has_active_pages"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Complete HTML tags stripped out of stored TITLES. A title is a short Data field
# (a name like "Acme Corp"), never markup — but it is org-user authored and gets
# interpolated into the reviewer's promotion-approve confirm, which frappe-ui's
# ConfirmDialog renders via v-html. Neutralizing at THIS write funnel (SAR-1
# server belt) means a stored title can never carry a runnable element no matter
# which writer set it; the SPA also escapes at the sink.
_TAG_RE = re.compile(r"<[^>]*>")

# Instruction shapes neutralized in stored BODIES. Bodies are markdown, so
# headings/fences/--- stay (legitimate structure); these are pure injection
# tokens: ignore-previous phrasing, a forged role prefix, a forged
# <available_skills> tag and the jarvis__ tool-call token (defanged so a
# stored body read back by jarvis__read_wiki can never carry a runnable
# tool instruction).
_BODY_NEUTRALIZE = (
	(re.compile(r"ignore\s+(?:all\s+|the\s+|any\s+)?previous", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"disregard\s+(?:all\s+|the\s+|any\s+)?(?:previous|above)", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"(?im)^(\s*)(?:system|assistant|developer)\s*:"), r"\1(sanitized):"),
	(re.compile(r"<\s*/?\s*available_skills\s*>", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"jarvis__"), "jarvis-"),
)


def _invalidate_has_pages_flag():
	frappe.cache().delete_value(WIKI_HAS_PAGES_CACHE_KEY)


class JarvisWikiPage(Document):
	def before_insert(self):
		# Must run BEFORE set_new_name (autoname field:slug freezes the
		# docname right after before_insert), so the audience suffix lands
		# in the name too. validate() runs too late for that.
		self._normalize_scope()
		self._apply_scope_slug_suffix()

	def validate(self):
		self._validate_slug()
		self._validate_scope()
		self._guard_scope_change()
		self._sanitize_untrusted_text()
		self._validate_lengths()

	def after_insert(self):
		_invalidate_has_pages_flag()

	def on_update(self):
		# Covers every save, the archive path included (archive is a status
		# flip through doc.save).
		_invalidate_has_pages_flag()

	def on_trash(self):
		_invalidate_has_pages_flag()

	def _sanitize_untrusted_text(self):
		"""Wiki text is org-user-authored and flows into prompts (the per-turn
		[Context:] clause inlines summaries; ``jarvis__read_wiki`` returns
		bodies), so instruction-shaped content is neutralized at THIS write
		funnel — every writer (ingest, update_wiki tool, SPA save) lands here."""
		if self.title:
			# Strip complete HTML tags, then drop any residual angle brackets, so a
			# stored title can NEVER carry markup — even a malformed/unterminated
			# tag (SAR-1 server belt). Titles are short Data, never HTML.
			t = _CONTROL_RE.sub("", str(self.title))
			self.title = _TAG_RE.sub("", t).replace("<", "").replace(">", "")
		if self.summary:
			s = _CONTROL_RE.sub("", str(self.summary))
			self.summary = SANITIZED_PLACEHOLDER if scan_instruction_injection(s) else s
		if self.body_md:
			body = _CONTROL_RE.sub("", str(self.body_md))
			for pattern, repl in _BODY_NEUTRALIZE:
				body = pattern.sub(repl, body)
			self.body_md = body

	def _normalize_scope(self):
		"""Fill scope defaults + drop off-scope target fields. NULL scope
		(pre-v2 rows) reads as Org everywhere, so it normalizes to Org here."""
		self.scope = (self.scope or "").strip() or "Org"
		if self.scope == "User":
			# The creator is the default audience of their own page.
			self.target_user = self.target_user or self.owner or frappe.session.user
			self.target_role = None
		elif self.scope == "Role":
			self.target_user = None
		else:
			self.target_role = None
			self.target_user = None

	def _validate_scope(self):
		self._normalize_scope()
		if self.scope not in SCOPES:
			frappe.throw(_("Scope must be one of {0}.").format(", ".join(SCOPES)))
		if self.scope == "Role" and not self.target_role:
			frappe.throw(_("Role-scope pages need a Target Role."))
		if self.scope == "User" and not self.target_user:
			frappe.throw(_("User-scope pages need a Target User."))

	def _scope_slug_suffix(self) -> str:
		"""The audience suffix a non-Org slug must carry (empty for Org)."""
		from jarvis.chat.entities import scrub

		if self.scope == "User" and self.target_user:
			local = scrub(str(self.target_user).split("@")[0])
			return f"--u-{local}" if local else ""
		if self.scope == "Role" and self.target_role:
			role = scrub(self.target_role)
			return f"--r-{role}" if role else ""
		return ""

	def _apply_scope_slug_suffix(self):
		"""Create-time only: suffix non-Org slugs (``--u-<localpart>`` /
		``--r-<role>``) so per-user/per-role pages never collide with the org
		page (or each other) on the shared slug namespace. Skipped when the
		caller already suffixed; base is trimmed to keep the total <= 140 and
		grammar-valid (scrub emits alnum-and-single-hyphen runs only)."""
		self.slug = (self.slug or "").strip().lower()
		suffix = self._scope_slug_suffix()
		if not self.slug or not suffix or self.slug.endswith(suffix):
			return
		base = self.slug[: MAX_SLUG_LEN - len(suffix)].rstrip("-")
		self.slug = f"{base}{suffix}"

	def _guard_scope_change(self):
		"""Only a System Manager may re-scope or re-target an existing page —
		anything else would let a page's audience be widened (or a personal
		page hijacked) by whoever can reach a save path. Runs regardless of
		ignore_permissions, because the SPA/tool writers save that way."""
		if self.is_new():
			return
		prev = frappe.db.get_value(
			self.doctype,
			self.name,
			["scope", "target_role", "target_user"],
			as_dict=True,
		)
		if not prev:
			return
		# Pre-v2 rows carry NULL scope; loading one normalizes it to Org,
		# which must not read as a change.
		prev_scope = (prev.scope or "").strip() or "Org"
		if (
			self.scope == prev_scope
			and (self.target_role or None) == (prev.target_role or None)
			and (self.target_user or None) == (prev.target_user or None)
		):
			return
		user = frappe.session.user
		if user == "Administrator" or "System Manager" in frappe.get_roles(user):
			return
		frappe.throw(
			_("Only a System Manager can change the scope or audience of an existing wiki page."),
			frappe.PermissionError,
		)

	def _validate_slug(self):
		self.slug = (self.slug or "").strip().lower()
		if not self.slug:
			frappe.throw(_("Slug is required."))
		if len(self.slug) > MAX_SLUG_LEN:
			frappe.throw(_("Slug must be at most {0} characters.").format(MAX_SLUG_LEN))
		if not SLUG_RE.match(self.slug):
			frappe.throw(
				_(
					"Slug must be lowercase letters and digits separated by "
					"single or double hyphens (e.g. customer--acme-corp)."
				)
			)

	def _validate_lengths(self):
		self.title = (self.title or "").strip()
		if self.summary and len(self.summary) > MAX_SUMMARY_LEN:
			frappe.throw(_("Summary must be at most {0} characters.").format(MAX_SUMMARY_LEN))
		if self.body_md and len(self.body_md) > MAX_BODY_LEN:
			frappe.throw(_("Body must be at most {0} characters.").format(MAX_BODY_LEN))
