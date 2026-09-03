"""Frappe boot_session hook.

Frappe builds the per-session ``bootinfo`` blob that the desk + page JS
reads from ``frappe.boot`` at page load. Apps register a single hook
function via ``hooks.boot_session`` to write their own keys onto that
blob. This file is that function for jarvis.

Keys we set:

- ``jarvis_onboarded`` - whether the customer has finished the Jarvis
  setup wizard, used by the desk's not-onboarded banner
  (``jarvis_onboarding_banner.bundle.js``) to decide whether to nag a
  System Manager toward ``/jarvis/onboarding``.
- ``jarvis_ready_reason`` - the ``reason`` from the same readiness check,
  set only when NOT ready. Lets desk surfaces tell "never onboarded"
  apart from "was onboarded, lost its AI connection" (``reason ==
  "llm_credentials"``) without a second round trip, so the banner can
  send the second case to the AI models settings pane instead of
  restarting the wizard.
- ``jarvis_has_been_ready`` - whether this workspace has EVER been
  confirmed chat-ready (``jarvis.account.has_been_chat_ready``). Some
  reasons alone are ambiguous: ``llm_pool_provisioning`` /
  ``llm_provisioning`` fire both for a brand-new tenant whose first sync
  is still pending AND for an established workspace whose apply-confirmed
  marker was cleared (e.g. disconnecting all models). This flag is the
  disambiguator the desk banner needs to pick the right CTA for the two.
- ``jarvis_has_access`` - whether the current user may reach Jarvis at
  all (``jarvis.permissions.has_jarvis_access``). Lets the desk's
  floating Jarvis button send an unauthorized user to
  ``/jarvis-no-access`` instead of opening the chat panel.
- ``jarvis_site_setup_complete`` - whether the site has a Company yet.
  The same banner also gates on this, so a fresh install running
  ERPNext's own setup wizard is not simultaneously nagged to set up
  Jarvis, which has nothing to operate until a company exists.
"""

import frappe


def set_jarvis_boot(bootinfo):
	"""Run once per session at page load. Adds jarvis-specific keys to the
	bootinfo blob so JS can branch on them without an extra round trip."""
	# Drives the desk's not-onboarded banner (jarvis_onboarding_banner.bundle.js).
	# Uses is_ready_for_chat rather than the lighter is_onboarded because the
	# SPA wizard now covers both signup AND the LLM-connect step (Phase 2
	# Task 5) - is_onboarded only reflects step 1 (admin api_key present) and
	# would mark a signed-up-but-not-connected customer as "done", silencing
	# the nag before setup is actually finished.
	try:
		from jarvis.account import is_ready_for_chat

		# Captured once: jarvis_ready_reason below reads the same dict rather
		# than calling is_ready_for_chat() a second time.
		readiness = is_ready_for_chat() or {}
		bootinfo.jarvis_onboarded = bool(readiness.get("ready"))
		bootinfo.jarvis_ready_reason = "" if bootinfo.jarvis_onboarded else (readiness.get("reason") or "")
	except Exception:
		bootinfo.jarvis_onboarded = True  # fail-safe: never nag on a boot error
		bootinfo.jarvis_ready_reason = ""

	# has_been_chat_ready() is its own fail-safe (returns False on error) and
	# reads the same lightweight raw-settings fields is_ready_for_chat's own
	# gates already read - a single extra SQL select, never a second admin
	# round trip - so this is a plain call, not folded into the try/except
	# above: a failure in is_ready_for_chat() must not also blank this flag,
	# and a failure here must not affect jarvis_onboarded/jarvis_ready_reason.
	try:
		from jarvis.account import has_been_chat_ready

		bootinfo.jarvis_has_been_ready = bool(has_been_chat_ready())
	except Exception:
		bootinfo.jarvis_has_been_ready = False  # fail-safe: never claim established on a boot error

	# Drives the desk's floating Jarvis button: an unauthorized user is routed
	# to /jarvis-no-access instead of the chat panel opening. Import kept
	# inside the try block (like the blocks above) so tests can patch
	# jarvis.permissions.has_jarvis_access without touching module load order.
	try:
		from jarvis.permissions import has_jarvis_access

		bootinfo.jarvis_has_access = bool(has_jarvis_access())
	except Exception:
		bootinfo.jarvis_has_access = False  # fail-closed; the no-access page self-heals

	# Gates the same not-onboarded nudge on the SITE being set up first.
	#
	# Jarvis operates the customer's ERP; on a site that has not completed setup
	# there is no ERP for it to operate yet. Nagging a fresh install to "set up
	# Jarvis" lands in the middle of ERPNext's own setup wizard, competing with
	# it for the same person's attention and pointing at a wizard whose whole
	# premise (a company to run) does not exist.
	#
	# Company count is the signal rather than System Settings.setup_complete:
	# it is what actually has to exist for Jarvis to be useful, and it stays
	# correct on a site whose setup flag was flipped by a fixture or a restore.
	# Guarded on the doctype because erpnext is not a hard dependency of jarvis.
	try:
		bootinfo.jarvis_site_setup_complete = bool(
			frappe.db.exists("DocType", "Company") and frappe.db.count("Company")
		)
	except Exception:
		# Match the jarvis_onboarded fail-safe above: when in doubt, stay quiet.
		# A nudge that cannot be dismissed by finishing setup is worse than a
		# missing one.
		bootinfo.jarvis_site_setup_complete = False

	# Whitelabel branding for the desk floating chat widget (Panel.vue / Widget.vue
	# read window.frappe.boot.* synchronously, so no flash). Blank => the widget
	# keeps the Jarvis defaults.
	try:
		bootinfo.jarvis_agent_name = frappe.db.get_single_value("Jarvis Settings", "agent_name") or ""
		bootinfo.jarvis_brand_logo_url = frappe.db.get_single_value("Jarvis Settings", "brand_logo") or ""
	except Exception:
		bootinfo.jarvis_agent_name = ""
		bootinfo.jarvis_brand_logo_url = ""
