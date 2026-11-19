"""Provider ids as the gateway names them, versus as this app and admin name them.

openclaw 2026.11 treats a few bare provider ids as its own official plugins, so fleet
renders those providers under ``<id>_api`` (``moonshot`` becomes ``moonshot_api``). A pin
such as ``moonshot/kimi-k2.6`` then names an id the 2026.11 gateway no longer has. Everything
else in the app keeps the plain id; this module is the one place that converts.

* Writing to the gateway (a session's model pin): ``model_ref_for_gateway`` renames the
  provider, and only when admin has told us the gateway runs openclaw 2026.11 or later
  (``native_runtime.gateway_is_v11``). With no version known, the plain id is sent, as before.
* Reading from the gateway (the ``modelProvider`` of a session row): ``provider_from_gateway``
  maps ``<id>_api`` back to the plain id whatever the version. No app id ends in ``_api``,
  so this is safe on a gateway that never renames.

Only the ids in ``WIRE_PROVIDER_IDS`` are touched. Pool ids such as ``moonshot-0`` and the
providers that are not renamed (``openai``, ``anthropic``, ``xai``, ``gemini``, ``google``,
``openrouter``, ``openai_compat``, ``zai_coding``, ``minimax``) pass through unchanged.
Keep the table in step with jarvis-fleet-agent ``openclaw_v11.WIRE_PROVIDER_IDS``."""

from jarvis import native_runtime

WIRE_PROVIDER_IDS = {
	"moonshot": "moonshot_api",
	"deepseek": "deepseek_api",
	"groq": "groq_api",
	"mistral": "mistral_api",
	"zai": "zai_api",
	"kimi": "kimi_api",
	"qwen": "qwen_api",
}
_FROM_WIRE = {wire: plain for plain, wire in WIRE_PROVIDER_IDS.items()}


def provider_to_gateway(provider, settings=None):
	"""The id to send the gateway for ``provider``: renamed on a known 2026.11+ gateway."""
	if (
		isinstance(provider, str)
		and provider in WIRE_PROVIDER_IDS
		and native_runtime.gateway_is_v11(settings)
	):
		return WIRE_PROVIDER_IDS[provider]
	return provider


def provider_from_gateway(provider):
	"""The app's id for a provider id read from the gateway."""
	return _FROM_WIRE.get(provider, provider) if isinstance(provider, str) else provider


def model_ref_for_gateway(provider, model, settings=None):
	"""``"<provider>/<model>"`` for the gateway, or the bare ``model`` when there is no
	provider. Takes the two parts, never a joined string: a bare model id can itself
	contain a slash (``deepseek/deepseek-r1`` on OpenRouter) and must not be renamed."""
	if provider and model:
		return f"{provider_to_gateway(provider, settings)}/{model}"
	return model
