"""Reset any site left on the removed Gemini chat SUBSCRIPTION.

Google discontinued consumer login-with-Google for Gemini on 2026-06-18 (the
gemini-cli OAuth client returns UNSUPPORTED_CLIENT); the subscription option was
removed 2026-08-19. A site whose ``Jarvis Settings`` still sits in a
direct-subscription auth mode (``oauth``/``subscription``) for ``llm_provider``
"Google Gemini" would break chat dispatch after this deploy -- the
"Google Gemini" -> "google-gemini-cli" agent mapping is gone, so the turn would
mis-route with "No API key found". Move it to ``api_key`` mode and clear the
stale OAuth account fields so the customer can add a Gemini API key instead.

No-op on every other configuration, including api-key Gemini. Written with
``db.set_single_value`` so it does NOT trigger the Jarvis Settings on_update
LLM re-sync during ``bench migrate``.
"""

import frappe

_DIRECT_SUBSCRIPTION_MODES = {"oauth", "subscription"}


def execute():
	mode = frappe.db.get_single_value("Jarvis Settings", "llm_auth_mode")
	provider = frappe.db.get_single_value("Jarvis Settings", "llm_provider")
	if mode not in _DIRECT_SUBSCRIPTION_MODES or provider != "Google Gemini":
		return
	frappe.db.set_single_value("Jarvis Settings", "llm_auth_mode", "api_key")
	frappe.db.set_single_value("Jarvis Settings", "llm_oauth_account_email", "")
	frappe.db.set_single_value("Jarvis Settings", "llm_oauth_connected_at", None)
