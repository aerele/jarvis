"""Pure serialization: Jarvis Settings.models -> (PoolSpec dict, api_keys, oauth_blobs).

Secrets are pulled OUT of the spec and keyed by a deterministic ref so the
rendered config stays secret-free.

All functions here are defensive: no function raises on bad input.
- get_password() calls use raise_exception=False (masked-empty → "").
- json.loads() is guarded; errors are reported via validate_models(), not raised.
"""

import json


def _get_password(doc, fieldname: str, raise_exception: bool = False) -> str:
	"""Read a password field from a doc (BaseDocument) or plain dict/frappe._dict.

	- BaseDocument rows support .get_password() which handles DB-stored encrypted
	  values and in-memory plaintext. raise_exception=False so masked-empty → "".
	- frappe._dict / plain dict rows (used in tests / in-memory) just have the
	  raw value; read it directly.

	FT4 hardening: if the field is non-empty but get_password raises, propagate
	the error rather than collapsing it to "". Collapsing a real decryption failure
	to blank would silently drop the credential and send an empty secret to admin.
	"""
	if callable(getattr(doc, "get_password", None)):
		try:
			return doc.get_password(fieldname, raise_exception=raise_exception) or ""
		except Exception:
			# Check if the raw in-memory field has content (masked) — if so,
			# this is a real decryption error, not "field unset".
			raw = getattr(doc, fieldname, None) or (doc.get(fieldname) if hasattr(doc, "get") else None)
			if raw and raw != "":
				raise  # Real decryption error: propagate
			return ""  # Field genuinely unset
	return (doc.get(fieldname) or "") if hasattr(doc, "get") else (getattr(doc, fieldname, "") or "")


def _key_ref(idx: int) -> str:
	return f"POOL_KEY_{idx}"


# Canonical provider ids follow the admin preset catalog vocabulary
# (jarvis_admin/billing/catalog.py). The onboarding UI stores a provider as
# either the catalog *id* (presets: "openai"/"gemini") or the dropdown *label*
# ("OpenAI"/"Google Gemini") depending on which mode built it; normalize both to
# one canonical id so the same pool built two ways pushes an identical provider
# to admin/Bifrost. `gemini` (NOT `google`) is canonical — matches the catalog.
_PROVIDER_ALIASES = {
	# dropdown labels -> id (must match frontend pool.js PROVIDER_LABELS)
	"openai": "openai",
	"anthropic": "anthropic",
	"google gemini": "gemini",
	"mistral": "mistral",
	"groq": "groq",
	"together ai": "together",
	"deepseek": "deepseek",
	"moonshot (kimi)": "moonshot",
	"openrouter": "openrouter",
	"ollama (local)": "ollama",
	"vllm (local)": "vllm",
	"openai-compatible": "openai_compat",
	# xAI Grok is a native Bifrost provider.
	"xai grok": "xai",
	# GLM / Z.ai is a first-class stored provider id ("zai"), same as every
	# other alias here: a dropdown label normalizes to ITS OWN canonical id,
	# not a different provider's. It has no native Bifrost provider, so it
	# rides the openai-compatible custom-endpoint path (base_url required,
	# see validate_models) - but that collapse happens ONLY at wire time, in
	# build_pool_payload's _WIRE_PROVIDER_OVERRIDES below. Collapsing here
	# (at storage time) used to permanently overwrite the stored provider
	# with "openai_compat", so a saved GLM row round-tripped back into the
	# UI as "OpenAI-Compatible" - base_url was the only surviving evidence
	# it was ever GLM. Fixed 2026-07-17.
	"glm / z.ai": "zai",
	# GLM Coding Plan is a SEPARATE z.ai product from pay-as-you-go "zai" above
	# - different balance, different endpoint (api.z.ai/api/coding/paas/v4).
	# A coding-plan key authenticates fine against the pay-as-you-go endpoint
	# but reports "insufficient balance" (z.ai error code 1113) there, which
	# reads as a dead key/account when it is really just the wrong endpoint.
	# Distinct stored id so the two never collide and each keeps its own
	# base_url; both collapse to the same openai-compatible wire transport
	# (see _WIRE_PROVIDER_OVERRIDES below) since Bifrost has no native
	# provider for either.
	"glm / z.ai (coding plan)": "zai_coding",
	# legacy id alias: the old frontend used "google" for Gemini
	"google": "gemini",
}


def normalize_provider(value: str) -> str:
	"""Map a stored provider label/alias to its canonical catalog id.

	Case-insensitive on the lookup; unknown values pass through lowercased
	(Bifrost provider ids are lowercase). Blank stays blank.

	This is the STORAGE-time normalization: it must be a true round-trip for
	every provider (label in -> its own id out -> same id back in unchanged),
	so a saved pool always reflects what the customer actually picked. Any
	collapsing of one provider's identity onto another's wire transport (e.g.
	"zai" riding the openai-compatible Bifrost transport) belongs in
	build_pool_payload's _wire_provider(), not here.
	"""
	v = (value or "").strip()
	if not v:
		return ""
	return _PROVIDER_ALIASES.get(v.lower(), v.lower())


# ---------------------------------------------------------------------------
# Wire-time-only provider collapsing (build_pool_payload). Distinct from
# normalize_provider(): a provider id can be first-class in STORAGE (so the
# UI shows its real name) while still riding a different provider's transport
# on the Bifrost wire payload (because Bifrost has no native support for it).
# "zai" and "zai_coding" (both GLM / Z.ai variants) are the entries today -
# both ride the openai-compatible custom-endpoint transport (base_url carries
# the real Z.ai endpoint - the standard or coding-plan one respectively).
# ---------------------------------------------------------------------------
_WIRE_PROVIDER_OVERRIDES = {
	"zai": "openai_compat",
	"zai_coding": "openai_compat",
}


def _wire_provider(canonical_id: str) -> str:
	"""Map a STORED canonical provider id to the id emitted on the Bifrost wire
	payload. Passthrough for every provider except the wire-only overrides above.
	"""
	return _WIRE_PROVIDER_OVERRIDES.get(canonical_id, canonical_id)


# ---------------------------------------------------------------------------
# Which providers the chat picker may offer CATALOG models for, beyond the exact
# rows the customer saved.
#
# The container serves a model id its pool spec never named, so a customer does
# not have to leave chat and re-save Settings to try another model on a provider
# they already configured. z.ai is the one exception, and it is not cosmetic:
# measured against the agent runtime's own resolver, an unnamed model inherits `maxTokens`
# and `params` from the provider entry but NOT `reasoning` or `compat`, and GLM's
# thinking suppression is `compat.thinkingFormat === "zai" && model.reasoning`.
# Offer an unnamed GLM id and the customer gets a blank assistant bubble (#526).
#
# This MIRRORS jarvis-fleet-agent `pool_render`'s `any_model` marker, which is
# what actually gates the container. Keep the two in step: offering a model the
# fleet render did not admit gives `model_not_found`, and because that is an
# explicit bad-id rejection rather than an upstream failure, native failover does
# not engage and the turn dies. That is the #498 shape. The fleet side is the
# authority; this is only the display half.
#
# Keyed on provider id AND resolved host: the tenant in #526 reached z.ai through
# the generic `openai_compat` row, so a provider-id-only test would miss the very
# pool that reported the bug. Matched on the registrable domain (host is it, or a
# dot-subdomain of it) so `evilz.ai` and `api.z.ai.attacker.test` do not match.
# ---------------------------------------------------------------------------
_ZAI_PROVIDER_IDS = frozenset({"zai", "zai_coding"})
_ZAI_DOMAINS = ("z.ai", "bigmodel.cn")


def _host_of(base_url: str) -> str:
	from urllib.parse import urlsplit

	try:
		return (urlsplit((base_url or "").strip()).hostname or "").lower()
	except ValueError:
		return ""


def is_zai_backed(provider: str, base_url: str = "") -> bool:
	"""True when this credential talks to z.ai (GLM), by provider id or by host."""
	if (provider or "").strip().lower() in _ZAI_PROVIDER_IDS:
		return True
	host = _host_of(base_url)
	return any(host == d or host.endswith("." + d) for d in _ZAI_DOMAINS)


def accepts_any_model(m) -> bool:
	"""True when the chat picker may offer catalog models for this pool row.

	False for a subscription row (its model list belongs to the upstream, not the
	api-key catalog) and for anything z.ai-backed.
	"""
	if _credential_type(m) == "subscription":
		return False
	return not is_zai_backed(_field(m, "provider"), _field(m, "base_url"))


def _model_accounts(m) -> list:
	"""Return a subscription model's accounts as a list of dicts.

	Accounts are stored as a JSON array string in the ENCRYPTED
	`subscription_accounts` Password field ON the model row (a child of the
	Jarvis Settings Single). We read it via _get_password so DB-backed rows are
	decrypted, then json.loads. Never raises: empty / malformed / non-list → [].

	Back-compat: in-memory rows / unit-test objects that still carry a plain
	`accounts` list attribute (the pre-migration grandchild shape) are honored
	when `subscription_accounts` is empty. Real persisted rows no longer have an
	`accounts` field, so production always takes the JSON path.
	"""
	try:
		raw = _get_password(m, "subscription_accounts")
	except Exception:
		raw = ""
	if raw:
		try:
			parsed = json.loads(raw)
		except Exception:
			return []
		return parsed if isinstance(parsed, list) else []
	# Legacy in-memory fallback (never a DB read — the grandchild is gone).
	legacy = getattr(m, "accounts", None)
	if legacy is None and hasattr(m, "get"):
		legacy = m.get("accounts")
	return list(legacy) if legacy else []


def _safe_json_loads(blob: str):
	"""Try to parse blob as JSON. Returns (parsed, error_str).

	error_str is None on success, a human-readable message on failure.
	FT4: also rejects blobs that parse successfully but are not a dict (e.g. lists).
	"""
	try:
		parsed = json.loads(blob)
		if not isinstance(parsed, dict):
			return None, f"oauth_blob must be a JSON object (dict), got {type(parsed).__name__}"
		return parsed, None
	except Exception as exc:
		return None, f"malformed oauth_blob JSON: {exc}"


# ---------------------------------------------------------------------------
# compute_auto_enable — kept for back-compat with test_jarvis_llm_pool.py
# ---------------------------------------------------------------------------


def compute_auto_enable(pool) -> bool:
	"""Legacy helper used by the old Jarvis LLM Pool on_update (kept for back-compat)."""
	return len([m for m in pool.models if m.enabled]) >= 2 or any(
		m.credential_type == "subscription" and _model_accounts(m) for m in pool.models if m.enabled
	)


# ---------------------------------------------------------------------------
# Derived mode flags — compute_pool_mode / compute_proxy_active
#
# These answer two DIFFERENT questions, and conflating them is what broke the
# default chat turn for BYO api-key pools. See compute_proxy_active.
# ---------------------------------------------------------------------------


def compute_pool_mode(settings) -> bool:
	"""Return True when this tenant's LLM config is a POOL.

	A pool is pushed to the fleet as a whole ``models[]`` spec through
	``/llm-pool``, not as one flat credential through ``/llm-creds``, and its
	readiness gate and installed-apps resync follow the pool leg. This is the
	predicate ``compute_proxy_active`` used to encode.

	True when ≥2 models are enabled OR (a preset is selected AND ≥1 model
	enabled) OR any enabled model is a chat subscription. An empty enabled-model
	list with a preset does NOT count as pool-valid.
	"""
	enabled = _enabled_models(settings)
	if len(enabled) >= 2 or (bool(settings.preset) and len(enabled) >= 1):
		return True
	# A chat-subscription model REQUIRES the cliproxy sidecar, which is
	# provisioned ONLY on the pool path. The DIRECT (single-model) path just
	# pushes llm-creds and has no cliproxy, so a lone subscription model would
	# never get its OAuth blob served and chat would fail despite a "connected"
	# UI. Force such a config onto the pool path. (#200 review #1)
	return has_subscription_model(settings)


def has_subscription_model(settings) -> bool:
	"""True when at least one ENABLED models[] row is a chat subscription."""
	return any(_credential_type(m) == "subscription" for m in _enabled_models(settings))


def compute_proxy_active(settings) -> bool:
	"""Return True when the fleet actually DEPLOYS a proxy sidecar pair (Bifrost
	+ CLIProxyAPI) in front of this tenant's agent container.

	Mirrors jarvis-fleet-agent's ``pool_render.is_byo_direct``, inverted. A pool
	whose every enabled model is a BYO **api key** now renders "agent-direct":
	each credential becomes its own ``models.providers.<id>`` entry INSIDE
	agent and agent drives failover natively through
	``agents.defaults.model.{primary,fallbacks}``. No sidecar is deployed at all,
	so there is no Bifrost to expand ``jarvis-pool`` and no cliproxy auth profile
	to report. A pool holding ANY enabled subscription still takes the
	Bifrost+cliproxy path, because cliproxy is what serves an OAuth blob.

	This returned True for ANY 2+-model pool, which was right while every pool
	had a Bifrost and became wrong the day agent-direct shipped: a pure
	api-key pool carried ``proxy_active=1`` with no proxy, so every unpinned
	"Auto" turn patched its agent session to the Bifrost-only ``jarvis-pool``
	placeholder and the container rejected it with ``model_not_found``. Being an
	explicit bad-id rejection rather than an upstream failure, agent's native
	failover did not even engage.

	``proxy_active`` now IMPLIES ``compute_pool_mode`` (a subscription forces
	pool mode) but is strictly narrower. Ask ``compute_pool_mode`` for "which
	sync / readiness leg does this tenant take".
	"""
	return has_subscription_model(settings)


def pool_primary_model(settings) -> str:
	"""The model id a pool tenant's container runs by DEFAULT, or "".

	jarvis-fleet-agent orders a pool's enabled credentials with
	``llm_proxy.render._common.ordered`` — ``(order, provider, model)`` — and
	renders entry 0 as ``agents.defaults.model.primary``, the rest as
	``fallbacks``. Returning entry 0's model keeps the bench's idea of the pool's
	primary in step with the container's.

	The id is BARE (``glm-4.6``), like every other model id this app hands
	agent — ``llm_model``, ``model_override``, the Settings model picker —
	and agent resolves a bare id against its registered providers. We
	deliberately do NOT rebuild the fleet's ``<provider>-<idx>/<model>``
	qualified ref: that id scheme belongs to the fleet's renderer, and a copy
	here would silently rot the moment its indexing changed.

	``order`` is the real key. Provider and model only break ties, and the
	fleet's tie-break runs on the *Bifrost* provider name (synthesized per model
	for custom-endpoint vendors), so the two can differ only in the ordering of
	rows that already share an ``order``.
	"""
	if not compute_pool_mode(settings):
		return ""
	for m in sorted(_enabled_models(settings), key=_fleet_sort_key):
		name = (_field(m, "model") or "").strip()
		if name:
			return name
	return ""


def _fleet_sort_key(m) -> tuple:
	provider = _wire_provider(normalize_provider(_field(m, "provider")))
	return (_int(_field(m, "order")), provider, _field(m, "model") or "")


def _enabled_models(settings) -> list:
	return [m for m in (settings.models or []) if m.enabled]


def _credential_type(m) -> str:
	"""Credential type of a models[] row; "api_key" when unset."""
	return _field(m, "credential_type") or "api_key"


def _field(m, fieldname: str, default: str = ""):
	"""Read a plain (non-secret) field off a models[] row — BaseDocument or dict."""
	if hasattr(m, fieldname):
		return getattr(m, fieldname)
	return m.get(fieldname, default) if hasattr(m, "get") else default


def _int(value) -> int:
	"""Best-effort int; a junk `order` must not blow up a sort (module contract)."""
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


# ---------------------------------------------------------------------------
# validate_models — collect clean human-readable errors; never raises
# ---------------------------------------------------------------------------


def validate_models(settings) -> list:
	"""Validate settings.models and return a list of human-readable error strings.

	Empty list means the configuration is clean.
	No exception is ever raised from this function.
	"""
	errors = []
	seen_account_refs = {}  # account_ref -> model index (for duplicate detection)

	# If models has rows but ALL are disabled → at least 1 must be enabled.
	if settings.models:
		enabled_count = sum(1 for m in settings.models if m.enabled)
		if enabled_count == 0:
			errors.append("at least 1 model must be enabled when models are configured")
			return errors  # Early exit — rest of validation is moot

	for i, m in enumerate(settings.models):
		label = f"Model[{i}] ({getattr(m, 'model', None) or m.get('model', '?')})"

		if not m.enabled:
			continue

		cred_type = (
			m.credential_type if hasattr(m, "credential_type") else m.get("credential_type", "api_key")
		)

		if cred_type == "api_key":
			# Blank api_key on an enabled model → dangling key_ref
			try:
				key_val = _get_password(m, "api_key")
			except Exception:
				errors.append(
					f"cannot read api_key for model '{getattr(m, 'model', None) or m.get('model', '?')}' (decryption error)"
				)
				continue
			if not key_val:
				errors.append(
					f"{label}: api_key is blank on an enabled model (would produce a dangling key_ref)"
				)

			# Custom-endpoint providers (OpenAI-Compatible shim / local vLLM /
			# GLM / Z.ai / GLM Coding Plan) are defined by their base_url;
			# without it build_pool_payload emits a provider with no endpoint
			# and every turn on that model fails. The provider is already
			# normalized to its canonical id at this point. "zai" and
			# "zai_coding" both need this exactly like "openai_compat" because
			# both ride the same wire transport (see pool_serialize._wire_provider)
			# - the requirement follows the transport, not the stored identity.
			prov = (m.provider if hasattr(m, "provider") else m.get("provider", "")) or ""
			base_url = (m.base_url if hasattr(m, "base_url") else m.get("base_url", "")) or ""
			if prov in ("openai_compat", "vllm", "zai", "zai_coding") and not base_url.strip():
				errors.append(f"{label}: provider '{prov}' requires a base_url (its custom endpoint)")

		elif cred_type == "subscription":
			accounts = _model_accounts(m)

			# Empty accounts list
			if not accounts:
				errors.append(f"{label}: enabled subscription model has no accounts configured")
				continue

			# Per-account upstream consistency + anthropic ToS check + malformed oauth_blob
			upstreams = set()
			for j, a in enumerate(accounts):
				acc_ref = a.account_ref if hasattr(a, "account_ref") else a.get("account_ref", "")
				upstream = a.upstream if hasattr(a, "upstream") else a.get("upstream", "")

				# Blank account_ref on enabled subscription account
				if not acc_ref:
					errors.append(f"{label} account[{j}]: subscription account is missing account_ref")

				# anthropic upstream is ToS-banned
				if upstream == "anthropic":
					errors.append(
						f"{label} account[{j}] ({acc_ref}): upstream='anthropic' is not permitted (ToS)"
					)
				# Only these subscription upstreams are supported (the pinned
				# cli-proxy-api image ships codex/antigravity/xai/kimi channels;
				# anthropic is ToS-banned above). The old child-doctype
				# Select(reqd) structurally enforced this; the JSON migration
				# dropped the guard, so a typo'd upstream would route to a
				# nonexistent provider. Re-enforce here. #200 review #6.
				elif upstream and upstream not in ("openai", "google", "xai", "kimi"):
					errors.append(
						f"{label} account[{j}] ({acc_ref}): unsupported upstream '{upstream}' (must be openai, google, xai, or kimi)"
					)

				upstreams.add(upstream)

				# Malformed oauth_blob — use _get_password so DB-backed masked rows
				# are decrypted correctly (avoids "malformed" error on re-save).
				try:
					blob_raw = (
						_get_password(a, "oauth_blob")
						if callable(getattr(a, "get_password", None))
						else (a.oauth_blob if hasattr(a, "oauth_blob") else a.get("oauth_blob", ""))
					)
				except Exception:
					errors.append(f"cannot read oauth_blob for account '{acc_ref}' (decryption error)")
					continue
				if blob_raw:
					_, parse_err = _safe_json_loads(blob_raw)
					if parse_err:
						errors.append(f"{label} account[{j}] ({acc_ref}): {parse_err}")
				elif acc_ref:
					# An enabled subscription account with an account_ref but NO
					# oauth_blob would be emitted into the pool spec by
					# build_pool_payload with no entry in the oauth_blobs map, so
					# cliproxy serves a credential-less account and every chat turn
					# 502s despite a "connected" UI. Reject it here instead.
					# (A blank incoming blob for an EXISTING account is refilled
					# from prior_blobs in save_llm_pool before this runs, so this
					# only fires when there is genuinely no stored credential.)
					errors.append(
						f"{label} account[{j}] ({acc_ref}): no OAuth credential "
						f"stored — reconnect this account to authorize"
					)

				# Duplicate account_ref detection
				if acc_ref:
					if acc_ref in seen_account_refs:
						errors.append(
							f"Duplicate account_ref '{acc_ref}' found in {label} account[{j}]"
							f" (also in Model[{seen_account_refs[acc_ref]}])"
						)
					else:
						seen_account_refs[acc_ref] = i

			# Mixed upstreams within one subscription model
			if len(upstreams) > 1:
				errors.append(
					f"{label}: all accounts must share the same upstream;"
					f" found mixed upstreams: {sorted(upstreams)}"
				)

	return errors


# ---------------------------------------------------------------------------
# build_pool_payload — re-sourced from settings (not pool single)
# ---------------------------------------------------------------------------


def build_pool_payload(settings):
	"""Return (spec_dict, api_keys, oauth_blobs).

	Reads settings.models (enabled rows) + settings.routing_mode / settings.preset.
	Pure; no I/O except get_password (which uses raise_exception=False).

	Hygiene guarantees:
	- Secrets never appear in the returned spec dict.
	- Subscription models never emit 'provider' or 'base_url'.
	- key_ref is only emitted when the secret is non-empty (no dangling refs).
	- json.loads on oauth_blob is guarded; errors are silently skipped here
	  (validate_models() catches them before save).
	"""
	models, api_keys, oauth_blobs = [], {}, {}
	key_idx = 0

	for m in settings.models:
		if not m.enabled:
			continue

		cred_type = (
			m.credential_type if hasattr(m, "credential_type") else m.get("credential_type", "api_key")
		)

		entry = {
			"model": m.model if hasattr(m, "model") else m.get("model"),
			"tier": (m.tier if hasattr(m, "tier") else m.get("tier")) or "strong",
			"order": (m.order if hasattr(m, "order") else m.get("order")) or 0,
			"enabled": True,
		}

		if cred_type == "subscription":
			# Omit provider and base_url for subscription models (avoids subscription_field_conflict)
			accounts = _model_accounts(m)
			serialized_accounts = []
			upstream = "openai"  # default; overridden by consistent account upstreams

			upstreams_seen = []
			for a in accounts:
				acc_ref = a.account_ref if hasattr(a, "account_ref") else a.get("account_ref", "")
				a_upstream = (a.upstream if hasattr(a, "upstream") else a.get("upstream", "")) or "openai"
				upstreams_seen.append(a_upstream)

				serialized_accounts.append(
					{
						"account_ref": acc_ref,
						"label": (a.label if hasattr(a, "label") else a.get("label", "")) or "",
					}
				)

				# Defensively guard json.loads; errors surfaced via validate_models
				blob_raw = (a.oauth_blob if hasattr(a, "oauth_blob") else a.get("oauth_blob", "")) or ""
				if blob_raw:
					parsed, _ = _safe_json_loads(blob_raw)
					if parsed is not None and acc_ref:
						oauth_blobs[acc_ref] = parsed

			# Use common upstream if consistent; fall back to "openai"
			if upstreams_seen:
				unique_upstreams = set(upstreams_seen)
				if len(unique_upstreams) == 1:
					upstream = upstreams_seen[0]
				# else: mixed — validate_models will catch this; emit "openai" as safe default

			rotation = (m.rotation if hasattr(m, "rotation") else m.get("rotation", "")) or "sticky"
			entry["subscription"] = {
				"upstream": upstream,
				"accounts": serialized_accounts,
				"rotation": rotation,
			}

		else:
			# api_key model: only emit key_ref when secret is actually present
			# Guard: if get_password raises (decrypt error), treat as blank —
			# no key_ref emitted; validate_models will already have flagged this.
			try:
				val = _get_password(m, "api_key")
			except Exception:
				val = ""
			if val:
				ref = _key_ref(key_idx)
				key_idx += 1
				entry["key_ref"] = ref
				api_keys[ref] = val
			# else: no key_ref emitted — dangling ref avoided; validate_models flags this
			# Also emit provider/base_url for api_key models
			provider = (m.provider if hasattr(m, "provider") else m.get("provider", "")) or ""
			base_url = (m.base_url if hasattr(m, "base_url") else m.get("base_url", "")) or ""
			if base_url:
				entry["base_url"] = base_url
			provider = normalize_provider(provider)
			if provider:
				# Wire-only collapse (e.g. "zai" -> "openai_compat"): the
				# stored/displayed identity stays "zai"; only the Bifrost
				# payload's provider id changes, so this MUST run last, right
				# before the entry is written - never earlier, and never at
				# storage time (see normalize_provider's docstring).
				entry["provider"] = _wire_provider(provider)

		models.append(entry)

	routing_mode = (
		settings.routing_mode if hasattr(settings, "routing_mode") else settings.get("routing_mode", "")
	) or "dynamic"

	# Tier-fallback: dynamic is meaningless without BOTH cheap + strong tiers
	tiers = {(m.get("tier") or "strong") for m in models}
	if routing_mode == "dynamic" and not ({"cheap", "strong"} <= tiers):
		routing_mode = "failover"  # fall back gracefully

	spec = {
		"name": _pool_name(),
		"routing_mode": routing_mode,
		"models": models,
	}
	if routing_mode == "dynamic":
		spec["classifier"] = {}  # pydantic defaults ClassifierConfig(); satisfies validate

	return spec, api_keys, oauth_blobs


def _pool_name() -> str:
	import frappe

	return (frappe.local.site or "jarvis-pool").split(".")[0]
