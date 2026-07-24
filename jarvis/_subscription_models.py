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

Subscription model ids MUST exist in the pinned cli-proxy-api image's embedded
catalogue: the ids are COMPILED INTO its Go binary and the image is
digest-pinned, so a model absent from it cannot be served whatever the catalog
says. jarvis_admin_v2 enforces that at save time. Verified 2026-07-22 against
v7.2.35: "grok-4.5" and "gpt-5.6" are ABSENT; "kimi-k2.7-code" is present.
"""

from __future__ import annotations

from collections.abc import Mapping

import frappe

_SEED_SUBSCRIPTION_MODELS: dict[str, list[str]] = {
	"OpenAI": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
	# NOT "gemini-3.1-flash": that id is absent from the pinned cliproxy image
	# (only the -image, -image-preview, -lite and -lite-preview variants are
	# compiled into its binary), so the subscription tier cannot serve it and a
	# customer choosing it silently misrouted to the pool primary. The api-key
	# tier still offers "gemini-3.1-flash" because those ids go to Google's real
	# API, which does serve it. jarvis_admin_v2 enforces this at save time.
	"Google Gemini": [
		"gemini-2.5-pro",
		"gemini-2.5-flash",
		"gemini-3.1-flash-lite",
	],
	"xAI Grok": ["grok-4.3", "grok-build-0.1"],
	"Kimi (Moonshot)": ["kimi-k2.7-code", "kimi-k2.6"],
}

_SEED_DEFAULT_MODEL: dict[str, str] = {
	"OpenAI": "gpt-5.5",
	"Google Gemini": "gemini-2.5-pro",
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
