"""Subscription-tier model catalogue.

The source of truth is the `Jarvis LLM Provider` doctype in jarvis_admin_v2,
read through admin_client.get_model_catalog() (Redis, else this site's
last-known-good snapshot; see jarvis.catalog_store). There is no model list in
this app. Add a model in the admin desk. On a site that has never reached the
admin the mappings are EMPTY: callers treat that as "catalog unavailable".

SUBSCRIPTION_MODELS and DEFAULT_MODEL keep their names and dict-like behaviour
deliberately (spec 6.3): chat/api.py and oauth/api.py import them at module
scope and read them on the chat hot path, and must not change.

Keys are the SUBSCRIPTION-surface provider label, which differs from the
api-key label for Kimi: "Kimi (Moonshot)" here vs admin's "Moonshot (Kimi)".
oauth/api.py:_coerce_subscription_model and oauth/providers.py both key off the
subscription form; emitting the other one silently coerces every Kimi model to
"" and defaults the picker to gpt-5.5.

xAI and Kimi subscription ids MUST exist in the pinned cli-proxy-api image
(compiled in; verified 2026-07-22 against v7.2.35: "grok-4.5" is ABSENT,
"kimi-k2.7-code" is present) and jarvis_admin_v2 enforces that at save time.
OpenAI is different: the Codex channel serves whatever OpenAI exposes to the
signed-in account. Verified live 2026-09-06 on that same pinned binary: the
suffixed gpt-5.6 ids and gpt-6-astra serve (bare "gpt-5.6" is an API-only
alias), while gpt-5.4 and gpt-5.4-mini were retired upstream and fail inside
cliproxy, so they are gone from this tier.
Claude subscriptions use the agent's native ``claude-cli`` backend and its
bundled model catalogue instead of CLIProxyAPI.
"""

from __future__ import annotations

from collections.abc import Mapping

import frappe

# Google Gemini has no subscription tier: its chat subscription was removed
# 2026-08-19 (Google discontinued consumer login-with-Google for Gemini). Gemini stays
# available via API key, served from the api_key-tier catalog.


def _subscription_rows() -> dict[str, list[dict]]:
	"""Subscription-surface label -> its subscription-tier model rows, sorted.

	Cached on frappe.local for the request: chat/api.py reads these inside
	send_message, so this must not re-hit Redis per lookup.
	"""
	cached = getattr(frappe.local, "_jarvis_sub_models", None)
	if cached is not None:
		return cached
	from jarvis import admin_client

	out = _rows_from(admin_client.get_model_catalog() or [])
	frappe.local._jarvis_sub_models = out
	return out


def _rows_from(catalog) -> dict[str, list[dict]]:
	out: dict[str, list[dict]] = {}
	for provider in catalog:
		rows = [m for m in provider.get("models") or [] if m.get("tier") == "subscription"]
		if not rows:
			continue
		rows.sort(key=lambda m: (m.get("sort_order") or 0, m.get("model_id") or ""))
		# R1: the subscription surface's label, which differs for Kimi.
		label = provider.get("subscription_label") or provider.get("label") or ""
		if label:
			out[label] = rows
	return out


class _LazyModelMap(Mapping):
	"""Dict-like view over the catalog, resolved on first read per request.

	A Mapping rather than a function so consumers (chat/api.py, oauth/api.py)
	keep working unchanged per spec 6.3.
	"""

	def __init__(self, builder):
		self._builder = builder

	def _data(self):
		return self._builder(_subscription_rows())

	def __getitem__(self, k):
		return self._data()[k]

	def __iter__(self):
		return iter(self._data())

	def __len__(self):
		return len(self._data())

	def __repr__(self):
		return repr(self._data())


def _models_from(rows) -> dict[str, list[str]]:
	return {label: [m["model_id"] for m in ms] for label, ms in rows.items()}


def _defaults_from(rows) -> dict[str, str]:
	out: dict[str, str] = {}
	for label, ms in rows.items():
		flagged = next((m for m in ms if m.get("is_default")), None)
		out[label] = (flagged or ms[0])["model_id"]
	return out


def mappings_from(catalog: list) -> tuple[dict[str, list[str]], dict[str, str]]:
	"""(subscription models, defaults) for an explicit catalog, bypassing the
	request cache: for a caller that already holds a freshly fetched catalog."""
	rows = _rows_from(catalog)
	return _models_from(rows), _defaults_from(rows)


SUBSCRIPTION_MODELS = _LazyModelMap(_models_from)
DEFAULT_MODEL = _LazyModelMap(_defaults_from)
