"""Subscription-tier model catalogue.

The source of truth is the `Jarvis LLM Provider` doctype in jarvis_admin_v2,
fetched via admin_client.get_model_catalog() (guest read, Redis cache, bundled
fallback). The literals below are the SEED and the degraded-mode floor only;
they are no longer edited to add a model. Add it in the admin desk.

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

# Google Gemini has no entry: its chat subscription was removed 2026-08-19 (Google
# discontinued consumer login-with-Google for Gemini). Gemini stays available via
# API key, which is served from the api_key-tier catalog, not this subscription seed.
_SEED_SUBSCRIPTION_MODELS: dict[str, list[str]] = {
	"OpenAI": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-astra", "gpt-5.5"],
	"Anthropic": [
		"claude-opus-5",
		"claude-sonnet-5",
		"claude-fable-5-1",
		"claude-fable-5",
		"claude-opus-4-8",
		"claude-opus-4-7",
		"claude-sonnet-4-6",
		"claude-opus-4-6",
	],
	"xAI Grok": ["grok-4.3", "grok-build-0.1"],
	"Kimi (Moonshot)": ["kimi-k2.7-code", "kimi-k2.6"],
}

_SEED_DEFAULT_MODEL: dict[str, str] = {
	"OpenAI": "gpt-5.6-terra",
	"Anthropic": "claude-opus-5",
	"xAI Grok": "grok-4.3",
	"Kimi (Moonshot)": "kimi-k2.7-code",
}


def _subscription_rows() -> dict[str, list[dict]]:
	"""Subscription-surface label -> its subscription-tier model rows, sorted.

	Cached on frappe.local for the request: chat/api.py reads these inside
	send_message, so this must not re-hit Redis per lookup.
	"""
	cached = getattr(frappe.local, "_jarvis_sub_models", None)
	if cached is not None:
		return cached
	from jarvis import admin_client

	out: dict[str, list[dict]] = {}
	for provider in admin_client.get_model_catalog() or []:
		rows = [m for m in provider.get("models") or [] if m.get("tier") == "subscription"]
		if not rows:
			continue
		rows.sort(key=lambda m: (m.get("sort_order") or 0, m.get("model_id") or ""))
		# R1: the subscription surface's label, which differs for Kimi.
		label = provider.get("subscription_label") or provider.get("label") or ""
		if label:
			out[label] = rows
	frappe.local._jarvis_sub_models = out
	return out


class _LazyModelMap(Mapping):
	"""Dict-like view over the catalog, resolved on first read per request.

	A Mapping rather than a function so consumers (chat/api.py, oauth/api.py)
	keep working unchanged per spec 6.3.
	"""

	def __init__(self, builder, seed):
		self._builder = builder
		self._seed = seed

	def _data(self):
		rows = _subscription_rows()
		return self._builder(rows) if rows else self._seed

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


SUBSCRIPTION_MODELS = _LazyModelMap(_models_from, _SEED_SUBSCRIPTION_MODELS)
DEFAULT_MODEL = _LazyModelMap(_defaults_from, _SEED_DEFAULT_MODEL)
