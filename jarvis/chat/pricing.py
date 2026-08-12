"""Admin-catalog $/token pricing for direct (BYO API-key) tenants.

The admin-owned model catalog (``admin_client.get_model_catalog()``) carries
per-model ``input_price_per_1m_usd`` / ``output_price_per_1m_usd`` (USD per
1,000,000 tokens; 0 = unpriced). This module flattens that catalog into a
flat ``{model_id: (input_per_1m, output_per_1m)}`` map so a direct tenant's
per-model token totals (``jarvis.chat.usage.tenant_wide_per_model_tokens``)
can be turned into a $ figure for the Billing/Metering endpoint
(``jarvis.account._direct_llm_usage``).

Mirrors ``jarvis._subscription_models._subscription_rows``: cached on
``frappe.local`` for the request, since the outer ``get_model_catalog()``
call is already Redis-cached and this is just a flatten, not a network call.
"""

from __future__ import annotations

import frappe

_PRICE_MAP_ATTR = "_jarvis_model_price_map"


def price_for_model(model_id: str) -> tuple[float, float]:
	"""``(input_per_1m_usd, output_per_1m_usd)`` for ``model_id``.

	``(0.0, 0.0)`` for an unknown or unpriced model. NEVER raises - mirrors
	``admin_client.get_model_catalog``'s never-raise contract, since this sits
	on the same Billing/Metering read path."""
	try:
		return _price_map().get(model_id, (0.0, 0.0))
	except Exception:
		frappe.logger().warning("price_for_model(%r) failed", model_id, exc_info=True)
		return (0.0, 0.0)


def _price_map() -> dict[str, tuple[float, float]]:
	cached = getattr(frappe.local, _PRICE_MAP_ATTR, None)
	if cached is not None:
		return cached
	price_map = _build_price_map()
	setattr(frappe.local, _PRICE_MAP_ATTR, price_map)
	return price_map


def _build_price_map() -> dict[str, tuple[float, float]]:
	from jarvis import admin_client

	price_map: dict[str, tuple[float, float]] = {}
	try:
		for provider in admin_client.get_model_catalog() or []:
			for model in (provider or {}).get("models") or []:
				model_id = model.get("model_id")
				if not model_id:
					continue
				price_map[model_id] = (
					float(model.get("input_price_per_1m_usd") or 0.0),
					float(model.get("output_price_per_1m_usd") or 0.0),
				)
	except Exception:
		frappe.logger().warning("price_for_model: catalog flatten failed", exc_info=True)
		return {}
	return price_map
