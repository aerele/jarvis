"""Per-agent minimum model (``Jarvis Settings.enforce_agent_min_model``).

Eligibility is pure over (settings, catalog); every DB-backed path runs against a
fake pool + catalog patched in through ``agent_models._pool_settings`` and
``admin_client.get_model_catalog``. The load-bearing guarantee is flag OFF: no
row, no gate and a push payload byte-identical to today's.
"""

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_models, agent_scheduler, agents_api
from jarvis.tests._agent_access import allow_listing_for

CHOICE = "Jarvis Agent Model Choice"
LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
FINDING = "Jarvis Agent Finding"
SETTINGS = "Jarvis Settings"
CONV = "Jarvis Conversation"
SESSION = "Jarvis Chat Session"

# Module-owned listings (Advanced floor): a registry agent may carry other modules'
# residual installs on a shared test site, which would turn a first install legacy.
AGENT = "amf-floor-auditor"
AGENT_B = "amf-floor-auditor-b"  # allowed, never installed: the allowed-listings leg
AGENT_C = "amf-floor-auditor-c"  # granted to nobody: never in the push payload
AGENTS = (AGENT, AGENT_B, AGENT_C)

OWNER = "amf-owner@example.com"
OTHER = "amf-other@example.com"
STRANGER = "amf-stranger@example.com"
ADMIN = "amf-admin@example.com"
CAST = (OWNER, OTHER, STRANGER, ADMIN)

CATALOG = [
	{
		"provider_id": "openai",
		"catalog_id": "openai",
		"label": "OpenAI",
		"subscription_label": "OpenAI",
		"models": [
			{"model_id": "gpt-frontier", "tier": "api_key", "sort_order": 0, "label": "GPT Frontier",
			 "capability_tier": "Frontier", "capability_rank": 3,
			 "input_price_per_1m_usd": 5, "output_price_per_1m_usd": 15},
			{"model_id": "gpt-adv", "tier": "api_key", "sort_order": 1,
			 "capability_tier": "Advanced", "capability_rank": 2},
			{"model_id": "gpt-std", "tier": "api_key", "sort_order": 2,
			 "capability_tier": "Standard", "capability_rank": 1},
			{"model_id": "gpt-raw", "tier": "api_key", "sort_order": 3, "capability_tier": "", "capability_rank": 0},
			{"model_id": "gpt-image-x", "tier": "api_key", "sort_order": 4,
			 "capability_tier": "Frontier", "capability_rank": 3},
			{"model_id": "gpt-sub", "tier": "subscription", "sort_order": 0,
			 "capability_tier": "Advanced", "capability_rank": 2},
		],
	},
	{
		"provider_id": "anthropic",
		"catalog_id": "anthropic",
		"label": "Anthropic",
		"subscription_label": "Anthropic",
		"models": [
			{"model_id": "claude-top", "tier": "subscription", "sort_order": 0,
			 "capability_tier": "Frontier", "capability_rank": 3},
		],
	},
	{
		"provider_id": "zai",
		"catalog_id": "zai",
		"label": "GLM / Z.ai",
		"models": [
			{"model_id": "glm-zai-only", "tier": "api_key", "sort_order": 0,
			 "capability_tier": "Frontier", "capability_rank": 3},
			# Same id on the aliased entry, ranked differently: a direct zai_coding
			# tenant must read its own entry's rank, never this one.
			{"model_id": "glm-coding", "tier": "api_key", "sort_order": 1,
			 "capability_tier": "Frontier", "capability_rank": 3},
		],
	},
	{
		"provider_id": "zai_coding",
		"catalog_id": "zai_coding",
		"label": "GLM / Z.ai (Coding Plan)",
		"models": [
			{"model_id": "glm-coding", "tier": "api_key", "sort_order": 0,
			 "capability_tier": "Advanced", "capability_rank": 2},
		],
	},
	{
		"provider_id": "moonshot",
		"catalog_id": "moonshot",
		"label": "Moonshot (Kimi)",
		"subscription_label": "Kimi (Moonshot)",
		"models": [
			{"model_id": "kimi-sub", "tier": "subscription", "sort_order": 0,
			 "capability_tier": "Advanced", "capability_rank": 2},
		],
	},
]  # fmt: skip

ADVANCED = frappe._dict(min_model=json.dumps({"tier": "Advanced"}))
STANDARD = frappe._dict(min_model=json.dumps({"tier": "Standard"}))


def _row(model, provider="openai", cred="api_key", order=0, upstream=None, enabled=1):
	r = frappe._dict(
		enabled=enabled,
		provider=provider if cred == "api_key" else "",
		model=model,
		tier="strong",
		order=order,
		base_url="",
		credential_type=cred,
		rotation="sticky",
		api_key="sk-test" if cred == "api_key" else "",
		subscription_accounts="",
	)
	if cred == "subscription":
		r.subscription_accounts = json.dumps(
			[{"upstream": upstream, "account_ref": f"acct-{model}", "label": "x", "oauth_blob": "{}"}]
		)
	return r


def _settings(*rows, preset="balanced", **extra):
	return frappe._dict(
		models=list(rows),
		preset=preset,
		routing_mode="failover",
		llm_pool_synced_at="2026-01-01 00:00:00",
		last_validated_pool_fp="",
		**extra,
	)


def _pool(*extra_rows):
	return _settings(
		_row("gpt-std", order=0),
		_row("gpt-adv", order=1),
		_row("gpt-frontier", order=2),
		*extra_rows,
	)


@contextmanager
def _env(settings=None, catalog=CATALOG):
	with ExitStack() as stack:
		stack.enter_context(patch.object(agent_models, "_pool_settings", return_value=settings or _pool()))
		stack.enter_context(patch("jarvis.admin_client.get_model_catalog", return_value=catalog))
		yield


@contextmanager
def _as(user):
	original = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original)


def _ensure_user(email, roles=()):
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	have = set(frappe.get_roles(email))
	missing = [r for r in ("Jarvis User", *roles) if r not in have and frappe.db.exists("Role", r)]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	frappe.db.commit()
	return email


def _flag(on: bool):
	frappe.db.set_single_value(SETTINGS, "enforce_agent_min_model", 1 if on else 0)
	frappe.db.commit()


def _choice(agent=AGENT, **values):
	doc = frappe.get_doc({"doctype": CHOICE, "agent": agent, **values})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _mk_listing(slug):
	if not frappe.db.exists(LISTING, slug):
		frappe.get_doc(
			{"doctype": LISTING, "agent_slug": slug, "title": f"AMF {slug}", "delivery": "delegate"}
		).insert(ignore_permissions=True)
	frappe.db.set_value(
		LISTING,
		slug,
		{
			"status": "Published",
			"nature": "Auditor",
			"operator_visibility": "available",
			"doctypes_required": "[]",
			"min_apps": "[]",
			"min_model": json.dumps({"tier": "Advanced"}),
		},
		update_modified=False,
	)


def _install(user=OWNER, agent=AGENT, enabled=True):
	with _as(user):
		name = agents_api.install_agent(agent)["data"]["name"]
	if enabled:
		frappe.db.set_value(INSTALLATION, name, "enabled", 1)
		frappe.db.commit()
	return name


def _refs(value):
	return json.loads(value) if isinstance(value, str) else value


class TestAgentModelEligibility(FrappeTestCase):
	"""Pure over (settings, catalog): no DB state."""

	def _eligible(self, listing, settings):
		return agent_models.eligible_models(listing, settings=settings, catalog=CATALOG)

	def test_tier_gate_orders_best_first_and_keeps_pool_order_within_a_rank(self):
		settings = _pool(
			_row("gpt-sub", cred="subscription", upstream="openai", order=6),
			_row("claude-top", cred="subscription", upstream="anthropic", order=7),
		)
		got = [(c["provider"], c["model"]) for c in self._eligible(ADVANCED, settings)]
		self.assertEqual(
			got,
			[
				("openai", "gpt-frontier"),
				("anthropic_cli", "claude-top"),
				("openai", "gpt-adv"),
				("", "gpt-sub"),
			],
		)
		self.assertIn(
			("openai", "gpt-std"), [(c["provider"], c["model"]) for c in self._eligible(STANDARD, settings)]
		)

	def test_uncurated_and_image_models_are_never_eligible(self):
		settings = _pool(_row("gpt-raw", order=3), _row("gpt-image-x", order=4))
		models = [c["model"] for c in self._eligible(STANDARD, settings)]
		self.assertNotIn("gpt-raw", models)
		self.assertNotIn("gpt-image-x", models)
		pool = agent_models.model_pool(settings=settings, catalog=CATALOG)
		self.assertNotIn("gpt-image-x", [c["model"] for c in pool])
		self.assertEqual(next(c for c in pool if c["model"] == "gpt-raw")["capability_rank"], 0)

	def _pairs(self, settings, model):
		return [
			(c["provider"], c["capability_rank"])
			for c in agent_models.model_pool(settings=settings, catalog=CATALOG)
			if c["model"] == model
		]

	def test_behind_the_proxy_only_the_first_row_per_model_id_counts(self):
		# A ChatGPT subscription puts Bifrost in the path: the lowest-order gpt-adv row
		# (uncurated groq) shadows the curated openai one.
		settings = _pool(
			_row("gpt-adv", provider="groq", order=-1),
			_row("gpt-sub", cred="subscription", upstream="openai", order=9),
		)
		self.assertEqual(self._pairs(settings, "gpt-adv"), [("groq", 0)])
		self.assertNotIn("gpt-adv", [c["model"] for c in self._eligible(ADVANCED, settings)])

	def test_agent_direct_pool_keeps_same_id_rows_on_different_providers(self):
		settings = _pool(_row("gpt-adv", provider="groq", order=-1))
		self.assertEqual(self._pairs(settings, "gpt-adv"), [("openai", 2), ("groq", 0)])

	def test_a_claude_plan_row_never_collapses(self):
		settings = _pool(
			_row("claude-top", provider="anthropic", order=-2),
			_row("claude-top", cred="subscription", upstream="anthropic", order=-1),
			_row("gpt-sub", cred="subscription", upstream="openai", order=9),
		)
		self.assertEqual(
			{p for p, _rank in self._pairs(settings, "claude-top")}, {"anthropic", "anthropic_cli"}
		)

	def test_subscription_rows_map_through_their_upstream(self):
		settings = _settings(
			_row("gpt-sub", cred="subscription", upstream="openai", order=0),
			_row("claude-top", cred="subscription", upstream="anthropic", order=1),
			_row("kimi-sub", cred="subscription", upstream="kimi", order=2),
		)
		by_model = {c["model"]: c for c in self._eligible(ADVANCED, settings)}
		self.assertEqual(
			(by_model["gpt-sub"]["provider"], by_model["gpt-sub"]["lane_hint"]), ("", "subscription")
		)
		self.assertEqual(by_model["gpt-sub"]["cost_note"], "Uses your OpenAI subscription allowance")
		self.assertEqual(
			(by_model["claude-top"]["provider"], by_model["claude-top"]["lane_hint"]),
			("anthropic_cli", "plan"),
		)
		self.assertEqual(by_model["kimi-sub"]["capability_rank"], 2)

	def test_api_key_cost_note_is_the_catalog_price(self):
		c = next(c for c in self._eligible(ADVANCED, _pool()) if c["model"] == "gpt-frontier")
		self.assertEqual(c["cost_note"], "$5.00 in / $15.00 out per 1M tokens")
		self.assertEqual(
			(c["capability_tier"], c["lane_hint"], c["label"]), ("Frontier", "api_key", "GPT Frontier")
		)

	def test_direct_mode_uses_the_configured_providers_catalog_on_its_lane(self):
		settings = _settings(_row("gpt-std"), preset="")
		got = [(c["provider"], c["model"]) for c in self._eligible(ADVANCED, settings)]
		self.assertEqual(got, [("openai", "gpt-frontier"), ("openai", "gpt-adv")])
		legacy = _settings(preset="", llm_provider="OpenAI", llm_model="gpt-std", llm_auth_mode="api_key")
		self.assertEqual([c["model"] for c in self._eligible(ADVANCED, legacy)], ["gpt-frontier", "gpt-adv"])

	def test_direct_mode_never_borrows_an_aliased_providers_models(self):
		# Admin's direct allowlist is the configured provider's own catalog entry.
		settings = _settings(_row("glm-coding", provider="GLM / Z.ai (Coding Plan)"), preset="")
		pool = agent_models.model_pool(settings=settings, catalog=CATALOG)
		self.assertEqual(
			[(c["provider"], c["model"], c["capability_rank"]) for c in pool],
			[("zai_coding", "glm-coding", 2)],
		)

	def test_pooled_api_key_row_reads_only_its_own_entrys_rank(self):
		settings = _pool(_row("glm-coding", provider="GLM / Z.ai (Coding Plan)", order=5))
		self.assertEqual(self._pairs(settings, "glm-coding"), [("openai_compat", 2)])

	def test_unreadable_catalog_is_unknown_not_empty(self):
		for catalog in ([], None, {"data": []}):
			with patch("jarvis.admin_client.get_model_catalog", return_value=catalog):
				self.assertEqual(
					agent_models.eligible_models(ADVANCED, settings=_pool()), agent_models.UNKNOWN
				)

	def test_unknown_tier_fails_closed(self):
		bogus = frappe._dict(min_model=json.dumps({"tier": "Ultra"}))
		self.assertEqual(self._eligible(bogus, _pool()), [])

	def test_requirement_copy_reads_naturally(self):
		self.assertIn("needs an Advanced-tier model", agent_models._no_model_message(ADVANCED))
		self.assertIn("needs a Standard-tier model", agent_models._blocked_message(STANDARD))


class AgentModelDBBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for slug in AGENTS:
			_mk_listing(slug)
		_ensure_user(OWNER)
		_ensure_user(OTHER)
		_ensure_user(STRANGER)
		_ensure_user(ADMIN, ("System Manager",))
		for slug in (AGENT, AGENT_B):
			for u in (OWNER, OTHER, ADMIN):
				allow_listing_for(slug, user=u)
		cls._saved = {
			f: frappe.db.get_single_value(SETTINGS, f)
			for f in (
				"agent_run_budget_monthly",
				"agent_catalog_dirty",
				"agent_skills_sync_status",
				"last_validated_pool_fp",
			)
		}
		frappe.db.set_single_value(SETTINGS, "agent_run_budget_monthly", 1000000)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._clean()
		for slug in AGENTS:
			frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
		frappe.db.set_single_value(SETTINGS, cls._saved)
		frappe.db.set_single_value(SETTINGS, "enforce_agent_min_model", 0)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self._clean()
		_flag(False)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clean()
		_flag(False)

	@staticmethod
	def _clean():
		frappe.db.rollback()
		for name in frappe.get_all(CHOICE, pluck="name"):
			frappe.delete_doc(CHOICE, name, force=True, ignore_permissions=True)
		for dt in (FINDING, RUN, ACTIVITY, INSTALLATION):
			for name in frappe.get_all(dt, filters={"agent": ["in", AGENTS]}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		for dt, field in ((CONV, "owner"), (SESSION, "user")):
			for name in frappe.get_all(dt, filters={field: ["in", CAST]}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _state(self, agent=AGENT):
		return frappe.db.get_value(
			CHOICE,
			agent,
			[
				"state",
				"provider",
				"model",
				"fallbacks",
				"note",
				"pushed_provider",
				"pushed_model",
				"fleet_unresolved",
				"admin_rejected",
			],
			as_dict=True,
		)


class TestFlagOffIsByteIdentical(AgentModelDBBase):
	def test_push_payload_is_unchanged_on_both_legs_with_rows_present(self):
		_install(OWNER, AGENT)  # leg 0: an enabled install; AGENT_B rides leg 1 (allowed)
		before = agent_catalog.build_agent_push_payload()
		slugs = {e["slug"] for e in before}
		self.assertTrue({f"agent-{AGENT}", f"agent-{AGENT_B}"} <= slugs)
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		_choice(AGENT_B, state="needs_model")
		after = agent_catalog.build_agent_push_payload()
		self.assertEqual(json.dumps(after, sort_keys=False), json.dumps(before, sort_keys=False))
		for entry in after:
			self.assertEqual(
				list(entry),
				["slug", "delivery", "tools_allow", "model", "timeout_s", "nature"],
				entry["slug"],
			)

	def test_install_creates_no_row_and_runs_are_not_gated(self):
		with _env(catalog=[]):  # even an unreadable catalog must not matter
			_install(OWNER, AGENT)
		self.assertFalse(frappe.db.exists(CHOICE, AGENT))
		self.assertEqual(agent_models.gate_run(AGENT), {"ok": True, "model_source": None})

	def test_setting_a_model_is_refused(self):
		with _env(), _as(OWNER), self.assertRaises(frappe.ValidationError):
			agent_models.set_agent_model(AGENT, "openai", "gpt-adv")


class TestPushKeys(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		_install(OWNER, AGENT)
		_flag(True)

	def _entries(self):
		return {e["slug"]: e for e in agent_catalog.build_agent_push_payload()}

	def test_pinned_row_adds_choice_fallbacks_and_ok_on_both_legs(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks=json.dumps(fb))
		_choice(AGENT_B, state="auto", provider="", model="gpt-sub", fallbacks="[]")
		entries = self._entries()
		self.assertEqual(
			entries[f"agent-{AGENT}"]["model_choice"], {"provider": "openai", "model": "gpt-adv"}
		)
		self.assertEqual(entries[f"agent-{AGENT}"]["model_fallbacks"], fb)
		self.assertEqual(entries[f"agent-{AGENT}"]["model_state"], "ok")
		self.assertEqual(entries[f"agent-{AGENT_B}"]["model_choice"], {"provider": "", "model": "gpt-sub"})

	def test_needs_model_is_blocked_without_a_choice(self):
		_choice(AGENT, state="needs_model", provider="openai", model="gpt-adv")
		entry = self._entries()[f"agent-{AGENT}"]
		self.assertEqual(entry["model_state"], "blocked")
		self.assertNotIn("model_choice", entry)

	def test_legacy_row_adds_no_keys(self):
		_choice(AGENT, state="legacy")
		self.assertNotIn("model_state", self._entries()[f"agent-{AGENT}"])

	def test_successful_push_stamps_pushed_models(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		_choice(AGENT_B, state="legacy", pushed_provider="openai", pushed_model="gpt-std")
		_choice(
			AGENT_C,
			state="chosen",
			provider="openai",
			model="gpt-adv",
			pushed_provider="openai",
			pushed_model="gpt-std",
		)
		# The dirty-clear guard refuses (version moves mid-push); stamping must not care.
		post = MagicMock(side_effect=lambda **kw: agents_api._bump_catalog_version())
		with patch("jarvis.admin_client.post_push_agent_skills", post):
			agents_api._enqueued_push_agent_skills()
		self.assertTrue(post.called)
		self.assertEqual((self._state().pushed_provider, self._state().pushed_model), ("openai", "gpt-adv"))
		self.assertEqual(
			(self._state(AGENT_B).pushed_provider or "", self._state(AGENT_B).pushed_model or ""), ("", "")
		)
		# Not in the payload (not deployed): its last stamp is left alone.
		self.assertEqual(
			(self._state(AGENT_C).pushed_provider, self._state(AGENT_C).pushed_model), ("openai", "gpt-std")
		)

	def test_platform_upgrade_refusal_is_plain_text(self):
		from jarvis.exceptions import AdminContractError

		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		err = AdminContractError(
			"platform_upgrade_pending: upgrade first", code="platform_upgrade_pending", http_status=409
		)
		with patch("jarvis.admin_client.post_push_agent_skills", side_effect=err):
			agents_api._enqueued_push_agent_skills()
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "agent_skills_sync_status", cache=False),
			"failed: platform upgrade pending",
		)
		self.assertFalse(self._state().pushed_model)


class TestApiGates(AgentModelDBBase):
	def test_doctype_has_no_docperm_so_rest_cannot_touch_it(self):
		self.assertEqual(frappe.get_all("DocPerm", filters={"parent": CHOICE}), [])
		_choice(AGENT, state="legacy")
		for user in (OWNER, ADMIN):
			self.assertFalse(frappe.has_permission(CHOICE, "read", user=user))
			self.assertFalse(frappe.has_permission(CHOICE, "write", user=user))
		with _as(ADMIN), self.assertRaises(frappe.PermissionError):
			frappe.client.get_list(CHOICE)
		with _as(ADMIN), self.assertRaises(frappe.PermissionError):
			frappe.client.set_value(CHOICE, AGENT, "model", "gpt-raw")

	def test_user_not_allowed_for_the_agent_is_refused(self):
		_flag(True)
		with _env(), _as(STRANGER):
			for call in (
				lambda: agent_models.get_agent_model(AGENT),
				lambda: agent_models.get_eligible_models(AGENT),
				lambda: agent_models.set_agent_model(AGENT, "openai", "gpt-adv"),
			):
				with self.assertRaises(frappe.PermissionError):
					call()
			with self.assertRaises(frappe.PermissionError):
				agent_models.reset_agent_model(AGENT)

	def test_set_validates_marks_dirty_logs_every_owner_and_never_pushes(self):
		_install(OWNER, AGENT)
		_install(OTHER, AGENT)
		_flag(True)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
		frappe.db.commit()
		enqueue, post, apply = MagicMock(), MagicMock(), MagicMock()
		with (
			_env(),
			_as(OWNER),
			patch("frappe.enqueue", enqueue),
			patch("jarvis.admin_client.post_push_agent_skills", post),
			patch.object(agents_api, "_enqueue_apply", apply),
		):
			with self.assertRaises(frappe.ValidationError):
				agent_models.set_agent_model(AGENT, "openai", "gpt-std")  # Standard < Advanced
			view = agent_models.set_agent_model(AGENT, "openai", "gpt-adv")
		self.assertEqual(view["state"], "chosen")
		self.assertEqual(view["choice"]["model"], "gpt-adv")
		self.assertEqual(view["pending_apply"], 1)
		row = self._state()
		self.assertEqual((row.state, row.provider, row.model), ("chosen", "openai", "gpt-adv"))
		fallbacks = _refs(row.fallbacks)
		self.assertLessEqual(len(fallbacks), 3)
		self.assertNotIn({"provider": "openai", "model": "gpt-adv"}, fallbacks)
		self.assertEqual(fallbacks, [{"provider": "openai", "model": "gpt-frontier"}])
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)), 1
		)
		for mock in (enqueue, post, apply):
			mock.assert_not_called()
		logged = frappe.get_all(
			ACTIVITY, filters={"action": "model_changed", "agent": AGENT}, fields=["owner", "detail"]
		)
		self.assertEqual({r.owner for r in logged}, {OWNER, OTHER})
		self.assertTrue(all(OWNER in r.detail for r in logged))

	def test_reset_is_admin_only_and_repicks_the_best(self):
		_flag(True)
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		with _env(), _as(OWNER), self.assertRaises(frappe.PermissionError):
			agent_models.reset_agent_model(AGENT)
		with _env(), _as(ADMIN):
			agent_models.reset_agent_model(AGENT)
		self.assertEqual((self._state().state, self._state().model), ("auto", "gpt-frontier"))


class TestInstall(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		_flag(True)

	def test_first_install_picks_the_best_eligible_model(self):
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
		frappe.db.commit()
		with _env():
			_install(OWNER, AGENT, enabled=False)
		row = self._state()
		self.assertEqual((row.state, row.provider, row.model), ("auto", "openai", "gpt-frontier"))
		self.assertEqual(_refs(row.fallbacks), [{"provider": "openai", "model": "gpt-adv"}])
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)), 1
		)

	def test_install_is_refused_when_nothing_qualifies(self):
		with _env(settings=_settings(_row("gpt-std"))):
			with _as(OWNER), self.assertRaises(frappe.ValidationError) as ctx:
				agents_api.install_agent(AGENT)
		self.assertIn("Advanced-tier", str(ctx.exception))
		self.assertFalse(frappe.db.exists(INSTALLATION, {"owner": OWNER, "agent": AGENT}))
		self.assertFalse(frappe.db.exists(CHOICE, AGENT))

	def test_second_installer_without_a_row_gets_legacy(self):
		_flag(False)
		_install(OWNER, AGENT)
		_flag(True)
		with _env():
			_install(OTHER, AGENT)
		self.assertEqual(self._state().state, "legacy")

	def test_existing_row_is_left_alone(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		with _env():
			_install(OWNER, AGENT)
		self.assertEqual((self._state().state, self._state().model), ("chosen", "gpt-adv"))

	def test_only_the_last_uninstall_deletes_the_row(self):
		with _env():
			first = _install(OWNER, AGENT)
			second = _install(OTHER, AGENT)
		with _as(OWNER):
			agents_api.uninstall_agent(first)
		self.assertTrue(frappe.db.exists(CHOICE, AGENT))
		with _as(OTHER):
			agents_api.uninstall_agent(second)
		self.assertFalse(frappe.db.exists(CHOICE, AGENT))

	def test_insert_race_keeps_the_winner(self):
		self.assertTrue(agent_models._insert_if_absent(AGENT, {"state": "legacy"}))
		messages = len(frappe.message_log)
		self.assertFalse(agent_models._insert_if_absent(AGENT, {"state": "auto", "model": "gpt-adv"}))
		self.assertEqual(self._state().state, "legacy")
		self.assertEqual(len(frappe.message_log), messages)  # no "already exists" leaks out
		# _upsert that loses the insert race updates the winner's row instead of failing.
		real = agent_models._get_row
		calls = []

		def racing(agent, for_update=False):
			calls.append(agent)
			return None if len(calls) == 1 else real(agent, for_update=for_update)

		with patch.object(agent_models, "_get_row", side_effect=racing):
			agent_models._upsert(AGENT, {"state": "chosen", "provider": "openai", "model": "gpt-adv"})
		self.assertEqual((self._state().state, self._state().model), ("chosen", "gpt-adv"))


class TestFlagTransition(AgentModelDBBase):
	def test_off_to_on_grandfathers_installed_agents_as_legacy(self):
		_install(OWNER, AGENT)
		enqueue = MagicMock()
		with patch("frappe.enqueue", enqueue):
			agent_models.on_enforcement_enabled()
		self.assertEqual(self._state().state, "legacy")
		self.assertFalse(frappe.db.exists(CHOICE, AGENT_B))  # allowed but never installed
		self.assertEqual(
			enqueue.call_args.args[0], "jarvis.chat.agent_models.refresh_catalog_then_revalidate"
		)

	def test_settings_on_update_runs_the_matching_transition(self):
		settings = frappe.get_single(SETTINGS)
		with (
			patch.object(agent_models, "on_enforcement_enabled") as on,
			patch.object(agent_models, "on_enforcement_disabled") as off,
		):
			settings.flags.agent_min_model_flip = None
			settings._maybe_flip_agent_min_model()
			settings.flags.agent_min_model_flip = "on"
			settings._maybe_flip_agent_min_model()
			settings.flags.agent_min_model_flip = "off"
			settings._maybe_flip_agent_min_model()
		on.assert_called_once()
		off.assert_called_once()

	def test_both_flips_prompt_an_apply_only_when_a_row_pins(self):
		_install(OWNER, AGENT)
		for flip in (agent_models.on_enforcement_disabled, agent_models.on_enforcement_enabled):
			frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
			frappe.db.commit()
			with patch("frappe.enqueue"):
				flip()  # only legacy rows (or none): nothing to apply
			self.assertEqual(
				frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)), 0
			)
		_choice(AGENT_B, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		for flip in (agent_models.on_enforcement_disabled, agent_models.on_enforcement_enabled):
			frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
			frappe.db.commit()
			with patch("frappe.enqueue"):
				flip()
			self.assertEqual(
				frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)), 1
			)


class TestRunGate(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		self.inst = _install(OWNER, AGENT)
		_flag(True)

	def test_ineligible_choice_moves_to_a_stored_fallback(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(
			AGENT, state="chosen", provider="openai", model="gpt-gone", fallbacks=json.dumps(fb),
			pushed_provider="openai", pushed_model="gpt-gone",
		)  # fmt: skip
		with _env():
			gate = agent_models.gate_run(AGENT)
		self.assertEqual(gate, {"ok": True, "model_source": "pending_apply"})
		row = self._state()
		self.assertEqual((row.state, row.model), ("changed", "gpt-frontier"))
		self.assertIn("gpt-gone", row.note)
		self.assertTrue(frappe.db.exists(ACTIVITY, {"action": "model_changed", "owner": OWNER}))

	def test_no_eligible_model_blocks_the_run(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-gone", fallbacks="[]")
		with _env():
			gate = agent_models.gate_run(AGENT)
		self.assertFalse(gate["ok"])
		self.assertEqual(self._state().state, "needs_model")
		with _env(), _as(OWNER), self.assertRaises(frappe.ValidationError) as ctx:
			agents_api.run_agent_now(self.inst)
		self.assertIn("Advanced-tier", str(ctx.exception))
		self.assertFalse(frappe.db.exists(RUN, {"installation": self.inst}))

	def test_model_source_follows_the_pushed_state(self):
		_choice(
			AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]",
			pushed_provider="openai", pushed_model="gpt-adv",
		)  # fmt: skip
		with _env():
			self.assertEqual(agent_models.gate_run(AGENT)["model_source"], "choice")
			frappe.db.set_value(CHOICE, AGENT, "pushed_model", "gpt-std")
			self.assertEqual(agent_models.gate_run(AGENT)["model_source"], "pending_apply")
			frappe.db.set_value(CHOICE, AGENT, "state", "legacy")
			self.assertEqual(agent_models.gate_run(AGENT)["model_source"], "pool_default")

	def test_scheduler_records_and_advances_instead_of_retrying(self):
		_choice(AGENT, state="needs_model")
		row = frappe.get_all(
			INSTALLATION,
			filters={"name": self.inst},
			fields=[
				"name", "owner", "run_as_user", "agent", "schedule_frequency", "schedule_time",
				"schedule_weekday", "schedule_day_of_month", "installable", "source_apps_json",
				"activation_state",
			],
		)[0]  # fmt: skip
		with (
			_env(),
			patch.object(agent_scheduler, "_record_failed") as record,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
		):
			agent_scheduler._sweep_one(row, frappe.utils.now_datetime(), "Administrator", set())
		dispatch.assert_not_called()
		advance.assert_called_once()
		self.assertIn("scheduled run skipped", record.call_args.args[1])
		self.assertIn("Advanced-tier", record.call_args.args[1])


class TestLaunchAndRunModel(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		self.inst = _install(OWNER, AGENT)
		_flag(True)
		_choice(
			AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]",
			pushed_provider="openai", pushed_model="gpt-adv",
		)  # fmt: skip

	def _launch(self, post):
		with (
			_env(),
			patch("jarvis.admin_client.post_agent_run", post),
			patch.object(agent_catalog, "registry_tools_allow", return_value=["jarvis__get_doc"]),
			_as(OWNER),
		):
			inst = frappe.get_doc(INSTALLATION, self.inst)
			return agent_scheduler._launch_audit(inst, trigger="scheduled")

	def _fleet_error(self, token):
		from jarvis.exceptions import AdminUnreachableError

		return MagicMock(
			side_effect=AdminUnreachableError(f"admin returned a 502 error: {token}: fleet says no")
		)

	def _run(self):
		return frappe.get_all(
			RUN, filters={"installation": self.inst}, fields=["*"], order_by="creation desc"
		)[0]

	def test_start_records_rendered_model_and_source_and_clears_unresolved(self):
		frappe.db.set_value(CHOICE, AGENT, "fleet_unresolved", 1)
		frappe.db.commit()
		post = MagicMock(
			return_value={
				"ok": True,
				"data": {"run_id": "x", "status": "queued", "model_rendered": "openai/gpt-adv"},
			}
		)
		result = self._launch(post)
		run = frappe.get_doc(RUN, result["run"])
		self.assertEqual((run.model_rendered, run.model_source), ("openai/gpt-adv", "choice"))
		self.assertEqual(self._state().fleet_unresolved, 0)

	def test_delegate_blocked_marks_needs_model(self):
		with self.assertRaises(frappe.ValidationError):
			self._launch(self._fleet_error("delegate_blocked"))
		self.assertEqual(self._state().state, "needs_model")
		self.assertEqual(self._run().status, "failed")

	def test_delegate_model_unresolved_flags_without_touching_the_choice(self):
		with self.assertRaises(frappe.ValidationError):
			self._launch(self._fleet_error("delegate_model_unresolved"))
		row = self._state()
		self.assertEqual((row.state, row.model, row.fleet_unresolved), ("chosen", "gpt-adv", 1))

	def test_render_context_missing_notes_platform_upgrade(self):
		with self.assertRaises(frappe.ValidationError):
			self._launch(self._fleet_error("render_context_missing"))
		row = self._state()
		self.assertEqual((row.state, row.note), ("chosen", "Platform upgrade pending"))
		self.assertIn("Platform upgrade pending", self._run().error)
		self._launch(MagicMock(return_value={"status": "queued", "model_rendered": "openai/gpt-adv"}))
		self.assertEqual(self._state().note or "", "")  # cleared once a run starts again

	def test_apply_in_progress_is_retryable_and_leaves_the_row(self):
		with self.assertRaises(frappe.ValidationError):
			self._launch(self._fleet_error("apply_in_progress"))
		row = self._state()
		self.assertEqual((row.state, row.note or "", row.fleet_unresolved), ("chosen", "", 0))
		self.assertIn("Try again", self._run().error)

	def test_model_used_and_below_min(self):
		cases = [
			({"provider": "openai", "model": "gpt-adv"}, "openai/gpt-adv", 0),
			({"provider": "openai", "model": "gpt-frontier"}, "openai/gpt-adv", 1),  # not what was rendered
			({"provider": "openai", "model": "gpt-std"}, "openai/gpt-std", 1),  # below the floor
		]
		for used, rendered, below in cases:
			run = frappe.get_doc(
				{
					"doctype": RUN,
					"agent": AGENT,
					"installation": self.inst,
					"trigger": "scheduled",
					"status": "completed",
					"model_rendered": rendered,
				}
			).insert(ignore_permissions=True)
			with _env():
				agent_models.record_model_used(run.name, {"model_used": used})
			got = frappe.db.get_value(RUN, run.name, ["model_used", "model_below_min"], as_dict=True)
			self.assertEqual((got.model_used, got.model_below_min), (f"openai/{used['model']}", below), used)
		_flag(False)
		frappe.db.set_value(RUN, run.name, {"model_used": "", "model_below_min": 0})
		agent_models.record_model_used(run.name, {"model_used": {"provider": "openai", "model": "gpt-raw"}})
		got = frappe.db.get_value(RUN, run.name, ["model_used", "model_below_min"], as_dict=True)
		self.assertEqual((got.model_used or "", got.model_below_min), ("", 0))  # flag off: untouched

	def test_flag_off_launch_keeps_todays_error_path_and_writes_no_model_fields(self):
		from jarvis.exceptions import AdminUnreachableError

		_flag(False)
		with self.assertRaises(AdminUnreachableError):
			self._launch(self._fleet_error("delegate_blocked"))
		self.assertEqual(self._state().state, "chosen")
		self.assertEqual(self._run().error, "agent-run dispatch failed; see Error Log")
		post = MagicMock(
			return_value={"ok": True, "data": {"status": "queued", "model_rendered": "openai/gpt-adv"}}
		)
		run = frappe.get_doc(RUN, self._launch(post)["run"])
		self.assertEqual((run.model_rendered or "", run.model_source or ""), ("", ""))


class TestRevalidation(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		_flag(True)

	def test_chosen_is_preserved_auto_upgrades_and_needs_model_recovers(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		_choice(AGENT_B, state="auto", provider="openai", model="gpt-adv", fallbacks="[]")
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
		frappe.db.commit()
		enqueue, post, apply = MagicMock(), MagicMock(), MagicMock()
		with (
			_env(),
			patch("frappe.enqueue", enqueue),
			patch("jarvis.admin_client.post_push_agent_skills", post),
			patch.object(agents_api, "_enqueue_apply", apply),
		):
			result = agent_models.revalidate_all()
		self.assertEqual(result, {"changed": 2, "failed": 0})  # AGENT: fallbacks refreshed
		self.assertEqual((self._state().state, self._state().model), ("chosen", "gpt-adv"))
		self.assertEqual((self._state(AGENT_B).state, self._state(AGENT_B).model), ("auto", "gpt-frontier"))
		for mock in (enqueue, post, apply):
			mock.assert_not_called()
		with _env():
			self.assertEqual(agent_models.revalidate_all(), {"changed": 0, "failed": 0})  # idempotent
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)), 1
		)
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "last_validated_pool_fp", cache=False),
			agent_models.validation_fingerprint(_pool(), CATALOG),
		)
		frappe.db.set_value(CHOICE, AGENT_B, {"state": "needs_model", "model": ""})
		frappe.db.commit()
		with _env():
			agent_models.revalidate_all()
		self.assertEqual((self._state(AGENT_B).state, self._state(AGENT_B).model), ("auto", "gpt-frontier"))

	def test_legacy_untouched_and_empty_catalog_is_a_noop(self):
		_choice(AGENT, state="legacy")
		_choice(AGENT_B, state="chosen", provider="openai", model="gpt-gone", fallbacks="[]")
		frappe.db.set_single_value(SETTINGS, "last_validated_pool_fp", "before")
		frappe.db.commit()
		with _env(catalog=[]):
			self.assertEqual(agent_models.revalidate_all(), {"skipped": agent_models.UNKNOWN})
		self.assertEqual(self._state(AGENT_B).state, "chosen")
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "last_validated_pool_fp", cache=False), "before"
		)
		with _env():
			agent_models.revalidate_all()
		self.assertEqual(self._state(AGENT).state, "legacy")
		self.assertEqual(
			(self._state(AGENT_B).state, self._state(AGENT_B).model), ("changed", "gpt-frontier")
		)

	def test_one_failing_row_does_not_stop_the_others(self):
		_choice(AGENT, state="needs_model")
		_choice(AGENT_B, state="needs_model")
		real = agent_models._revalidate_one

		def flaky(agent, pool, fp):
			if agent == AGENT:
				raise RuntimeError("boom")
			return real(agent, pool, fp)

		frappe.db.set_single_value(SETTINGS, "last_validated_pool_fp", "before")
		frappe.db.commit()
		with _env(), patch.object(agent_models, "_revalidate_one", side_effect=flaky):
			result = agent_models.revalidate_all()
		self.assertEqual(result, {"changed": 1, "failed": 1})
		self.assertEqual(self._state(AGENT).state, "needs_model")
		self.assertEqual(self._state(AGENT_B).state, "auto")
		# A failed row keeps the pool trigger armed.
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "last_validated_pool_fp", cache=False), "before"
		)

	def test_pool_or_catalog_fingerprint_triggers_revalidation(self):
		settings = _pool()
		with _env(settings=settings), patch.object(agent_models, "enqueue_revalidation") as enq:
			agent_models.check_pool_fingerprint()
			enq.assert_called_once()
			enq.reset_mock()
			settings.last_validated_pool_fp = agent_models.validation_fingerprint(settings, CATALOG)
			agent_models.check_pool_fingerprint()
			enq.assert_not_called()
		# A catalog change a running (deduped) re-validation swallowed still shows up.
		with (
			_env(settings=settings, catalog=CATALOG[:1]),
			patch.object(agent_models, "enqueue_revalidation") as enq,
		):
			agent_models.check_pool_fingerprint()
			enq.assert_called_once()
		_flag(False)
		with _env(settings=_pool()), patch.object(agent_models, "enqueue_revalidation") as enq:
			agent_models.check_pool_fingerprint()
			enq.assert_not_called()

	def test_catalog_change_triggers_revalidation(self):
		from jarvis.catalog_store import MODELS

		with (
			patch.object(
				MODELS, "_fetch_from_admin", return_value=[{"provider_id": "amf-probe", "models": []}]
			),
			patch.object(MODELS, "_save_snapshot", return_value=True),
			patch.object(MODELS, "_cache"),
			patch.object(agent_models, "enqueue_revalidation") as enq,
		):
			MODELS.refresh(force=True)
		enq.assert_called_once()


class TestAdminVerdict(AgentModelDBBase):
	"""Admin reports on an Apply which bench-sent refs it refused; the bench stamps
	what was delivered and never re-picks a refused ref under the same pool + catalog."""

	def setUp(self):
		super().setUp()
		_install(OWNER, AGENT)
		_flag(True)

	def _apply(self, rejected, blocked=()):
		response = {
			"ok": True,
			"model_choices": {
				"rejected": [{"slug": f"agent-{AGENT}", "models": rejected}],
				"blocked": list(blocked),
			},
		}
		with _env(), patch("jarvis.admin_client.post_push_agent_skills", return_value=response):
			agents_api._enqueued_push_agent_skills()

	def test_refused_primary_moves_the_row_to_what_admin_delivered(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(AGENT, state="auto", provider="openai", model="gpt-adv", fallbacks=json.dumps(fb))
		self._apply(["openai/gpt-adv"])
		row = self._state()
		self.assertEqual((row.state, row.model), ("changed", "gpt-frontier"))
		self.assertEqual((row.pushed_provider, row.pushed_model), ("openai", "gpt-frontier"))
		self.assertEqual(agent_models._source(row), "fallback")  # not pending: matches the fleet
		with _env(), _as(OWNER), self.assertRaises(frappe.ValidationError):
			agent_models.set_agent_model(AGENT, "openai", "gpt-adv")

	def test_blocked_row_needs_a_model_until_the_pool_or_catalog_changes(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(AGENT, state="auto", provider="openai", model="gpt-adv", fallbacks=json.dumps(fb))
		self._apply(["openai/gpt-adv", "openai/gpt-frontier"], blocked=[f"agent-{AGENT}"])
		row = self._state()
		self.assertEqual(
			(row.state, row.pushed_provider or "", row.pushed_model or ""), ("needs_model", "", "")
		)
		self.assertIn("gpt-adv", row.note)
		with _env():
			agent_models.revalidate_all()
			self.assertFalse(agent_models.gate_run(AGENT)["ok"])
		self.assertEqual(self._state().state, "needs_model")  # no loop back to a refused model
		with _env(catalog=[*CATALOG, {"provider_id": "amf-new", "models": []}]):
			agent_models.revalidate_all()
		self.assertEqual((self._state().state, self._state().model), ("auto", "gpt-frontier"))

	def test_refusal_expires_after_a_later_successful_sync_with_an_identical_fingerprint(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(AGENT, state="auto", provider="openai", model="gpt-adv", fallbacks=json.dumps(fb))
		self._apply(["openai/gpt-adv", "openai/gpt-frontier"], blocked=[f"agent-{AGENT}"])
		settings = _pool()
		with _env(settings=settings):
			agent_models.revalidate_all()
			self.assertEqual(self._state().state, "needs_model")
			settings.last_validated_pool_fp = frappe.db.get_single_value(
				SETTINGS, "last_validated_pool_fp", cache=False
			)
			with patch.object(agent_models, "enqueue_revalidation") as enq:
				agent_models.check_pool_fingerprint()
				enq.assert_not_called()  # same pool, catalog and sync: nothing to do
				# The pool sync admin was missing lands; the fingerprint is unchanged.
				settings.llm_pool_synced_at = "2026-09-28 12:00:00"
				agent_models.check_pool_fingerprint()
				enq.assert_called_once()
			agent_models.revalidate_all()
		row = self._state()
		self.assertEqual((row.state, row.model, row.admin_rejected), ("auto", "gpt-frontier", None))

	def test_admin_reset_forgets_a_refusal(self):
		_choice(AGENT, state="auto", provider="openai", model="gpt-frontier", fallbacks="[]")
		self._apply(["openai/gpt-frontier"], blocked=[f"agent-{AGENT}"])
		with _env(), _as(ADMIN):
			agent_models.reset_agent_model(AGENT)
		row = self._state()
		self.assertEqual((row.state, row.model, row.admin_rejected), ("auto", "gpt-frontier", None))

	def test_a_second_refusal_under_the_same_scope_adds_to_the_first(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		_choice(AGENT, state="auto", provider="openai", model="gpt-adv", fallbacks=json.dumps(fb))
		self._apply(["openai/gpt-adv"])
		self._apply(["openai/gpt-frontier"], blocked=[f"agent-{AGENT}"])
		self.assertEqual(
			json.loads(self._state().admin_rejected)["refs"],
			[{"provider": "openai", "model": "gpt-adv"}, {"provider": "openai", "model": "gpt-frontier"}],
		)

	def test_a_row_deleted_mid_push_does_not_undo_the_other_stamps(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		payload = [
			{
				"slug": f"agent-{AGENT}",
				"model_choice": {"provider": "openai", "model": "gpt-adv"},
				"model_fallbacks": [],
			},
			{
				"slug": "agent-amf-ghost",
				"model_choice": {"provider": "openai", "model": "gpt-adv"},
				"model_fallbacks": [],
			},
		]
		real = frappe.get_all

		def with_ghost(doctype, *args, **kwargs):
			rows = real(doctype, *args, **kwargs)
			return ["amf-ghost", *rows] if doctype == CHOICE and kwargs.get("pluck") == "name" else rows

		with _env(), patch.object(frappe, "get_all", side_effect=with_ghost):
			agent_models.stamp_pushed(payload, None)
		self.assertEqual(self._state().pushed_model, "gpt-adv")

	def test_refusal_scope_is_taken_before_the_push(self):
		# A pool apply confirms while admin checks the choices: the refusal must carry
		# the sync BEFORE it, so that very sync expires it.
		_choice(AGENT, state="auto", provider="openai", model="gpt-adv", fallbacks="[]")
		settings = _pool()
		before = agent_models._sync_marker(settings)
		response = {
			"model_choices": {
				"rejected": [{"slug": f"agent-{AGENT}", "models": ["openai/gpt-adv"]}],
				"blocked": [f"agent-{AGENT}"],
			}
		}

		def push_while_the_pool_syncs(**kwargs):
			settings.llm_pool_synced_at = "2026-09-28 12:30:00"
			return response

		with (
			_env(settings=settings),
			patch("jarvis.admin_client.post_push_agent_skills", side_effect=push_while_the_pool_syncs),
		):
			agents_api._enqueued_push_agent_skills()
			row = self._state()
			self.assertEqual(json.loads(row.admin_rejected)["sync"], before)
			self.assertEqual(agent_models._active_refusal(row), [])  # already expired by that sync

	def test_verdict_for_a_row_that_moved_mid_push_only_records_the_refusal(self):
		_choice(AGENT, state="chosen", provider="openai", model="gpt-frontier", fallbacks="[]")
		entry = {
			"slug": f"agent-{AGENT}",
			"model_choice": {"provider": "openai", "model": "gpt-adv"},
			"model_fallbacks": [],
			"model_state": "ok",
		}
		response = {
			"model_choices": {
				"rejected": [{"slug": entry["slug"], "models": ["openai/gpt-adv"]}],
				"blocked": [entry["slug"]],
			}
		}
		with _env():
			agent_models.stamp_pushed([entry], response)
		row = self._state()
		self.assertEqual((row.state, row.model, row.pushed_model or ""), ("chosen", "gpt-frontier", ""))
		self.assertEqual(json.loads(row.admin_rejected)["refs"], [{"provider": "openai", "model": "gpt-adv"}])


class TestRunErrorsNeverStrandARun(AgentModelDBBase):
	def setUp(self):
		super().setUp()
		self.inst = _install(OWNER, AGENT)
		_flag(True)
		_choice(AGENT, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]",
			pushed_provider="openai", pushed_model="gpt-adv")  # fmt: skip

	def _run(self, status=None):
		filters = {"installation": self.inst}
		if status:
			filters["status"] = status
		return frappe.get_all(RUN, filters=filters, fields=["*"], order_by="creation desc")[0]

	def test_a_failing_row_update_still_fails_the_run_and_tears_down_its_session(self):
		from jarvis.chat import agent_runs
		from jarvis.exceptions import AdminUnreachableError

		fleet = AdminUnreachableError("admin returned a 502 error: delegate_blocked: fleet says no")
		with (
			_env(),
			patch("jarvis.admin_client.post_agent_run", side_effect=fleet),
			patch.object(agent_catalog, "registry_tools_allow", return_value=["jarvis__get_doc"]),
			patch.object(agent_models, "handle_run_error", side_effect=frappe.QueryTimeoutError("lock wait")),
			patch.object(agent_runs, "teardown_run_session") as teardown,
			_as(OWNER),
		):
			with self.assertRaises(AdminUnreachableError):  # the fleet error, not the lock wait
				agent_scheduler._launch_audit(frappe.get_doc(INSTALLATION, self.inst), trigger="scheduled")
		run = self._run()
		self.assertEqual((run.status, run.error), ("failed", "agent-run dispatch failed; see Error Log"))
		teardown.assert_called_once_with(run.session_key)

	def test_a_failing_model_used_record_never_stops_the_poll_terminalizing(self):
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": AGENT,
				"installation": self.inst,
				"trigger": "scheduled",
				"status": "running",
				"started_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		row = frappe._dict(name=run.name, started_at=run.started_at)
		state = {
			"status": "failed",
			"error": "boom",
			"model_used": {"provider": "openai", "model": "gpt-adv"},
		}
		with patch.object(agent_models, "record_model_used", side_effect=RuntimeError("db")):
			self.assertTrue(agent_scheduler._reconcile_polled_run(row, state, frappe.utils.now_datetime()))
		self.assertEqual(frappe.db.get_value(RUN, run.name, "status"), "failed")

	def test_poll_and_backfill_record_model_used(self):
		now = frappe.utils.now_datetime()
		running = frappe.get_doc(
			{"doctype": RUN, "agent": AGENT, "installation": self.inst, "trigger": "scheduled",
			 "status": "running", "started_at": now, "model_rendered": "openai/gpt-adv"}
		).insert(ignore_permissions=True)  # fmt: skip
		done = frappe.get_doc(
			{"doctype": RUN, "agent": AGENT, "installation": self.inst, "trigger": "scheduled",
			 "status": "completed", "model_source": "choice", "model_rendered": "openai/gpt-adv",
			 "finished_at": frappe.utils.add_to_date(now, minutes=-5)}
		).insert(ignore_permissions=True)  # fmt: skip
		frappe.db.commit()
		used = {"provider": "openai", "model": "gpt-adv"}
		with _env():
			agent_scheduler._reconcile_polled_run(
				frappe._dict(name=running.name, started_at=now),
				{"status": "running", "model_used": used},
				now,
			)
			status = MagicMock(return_value={"ok": True, "data": {"status": "done", "model_used": used}})
			with patch("jarvis.admin_client.get_agent_run_status", status):
				self.assertEqual(agent_scheduler._backfill_model_used(now), 1)
			status.assert_called_once_with(done.name)
			_flag(False)
			with patch("jarvis.admin_client.get_agent_run_status") as off:
				agent_scheduler._backfill_model_used(now)
			off.assert_not_called()  # flag off: no extra admin traffic
		for name in (running.name, done.name):
			self.assertEqual(frappe.db.get_value(RUN, name, "model_used"), "openai/gpt-adv")

	def test_delegate_blocked_with_a_pending_apply_keeps_the_choice(self):
		frappe.db.set_value(CHOICE, AGENT, "pushed_model", "gpt-std")
		frappe.db.commit()
		with _env():
			agent_models.handle_run_error(AGENT, "delegate_blocked")
		self.assertEqual((self._state().state, self._state().model), ("chosen", "gpt-adv"))

	def test_unresolved_model_that_is_no_longer_eligible_degrades(self):
		fb = [{"provider": "openai", "model": "gpt-frontier"}]
		frappe.db.set_value(
			CHOICE, AGENT, {"model": "gpt-gone", "pushed_model": "gpt-gone", "fallbacks": json.dumps(fb)}
		)
		frappe.db.commit()
		with _env():
			agent_models.handle_run_error(AGENT, "delegate_model_unresolved")
		row = self._state()
		self.assertEqual((row.state, row.model, row.fleet_unresolved), ("changed", "gpt-frontier", 0))


class TestHooksAndGates(AgentModelDBBase):
	def test_non_admin_may_set_a_model_only_on_a_published_listing(self):
		_flag(True)
		frappe.db.set_value(LISTING, AGENT, "status", "Deprecated")
		frappe.db.commit()
		try:
			with _env(), _as(OWNER), self.assertRaises(frappe.PermissionError):
				agent_models.set_agent_model(AGENT, "openai", "gpt-adv")
		finally:
			_mk_listing(AGENT)
			frappe.db.commit()

	def test_below_min_ignores_eligibility_when_no_floor_is_declared(self):
		inst = _install(OWNER, AGENT)
		_flag(True)
		run = frappe.get_doc(
			{"doctype": RUN, "agent": AGENT, "installation": inst, "trigger": "scheduled",
			 "status": "completed", "model_rendered": "openai/gpt-std"}
		).insert(ignore_permissions=True)  # fmt: skip
		frappe.db.set_value(LISTING, AGENT, "min_model", None)
		frappe.db.commit()
		try:
			with _env(settings=_settings(_row("gpt-std"))):
				agent_models.record_model_used(
					run.name, {"model_used": {"provider": "openai", "model": "gpt-std"}}
				)
			self.assertEqual(frappe.db.get_value(RUN, run.name, "model_below_min"), 0)
		finally:
			_mk_listing(AGENT)
			frappe.db.commit()

	def test_listing_sync_enqueues_revalidation_only_when_a_requirement_moved(self):
		_flag(True)
		slug = "close-auditor"
		agent_catalog.sync_agent_listings()
		try:
			with patch.object(agent_models, "enqueue_revalidation") as enq:
				agent_catalog.sync_agent_listings()
				enq.assert_not_called()
				frappe.db.set_value(LISTING, slug, "min_model", json.dumps({"tier": "Standard"}))
				frappe.db.commit()
				agent_catalog.sync_agent_listings()
				enq.assert_called_once()
		finally:
			for listing in AGENTS:  # the sync deprecates listings outside the registry
				_mk_listing(listing)
			frappe.db.commit()

	def test_saving_the_same_settings_doc_again_after_a_flip_is_not_a_timestamp_mismatch(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

		_choice(AGENT_B, state="chosen", provider="openai", model="gpt-adv", fallbacks="[]")
		settings = frappe.get_single(SETTINGS)
		with (
			patch.object(JarvisSettings, "_on_update_unified_llm"),
			patch.object(JarvisSettings, "_on_update_single_model_legacy"),
			patch("frappe.enqueue"),
		):
			for value in (1, 0, 1):  # each flip marks the catalog dirty (moves `modified`)
				settings.enforce_agent_min_model = value
				settings.save(ignore_permissions=True)
				self.assertEqual(
					frappe.utils.cint(
						frappe.db.get_single_value(SETTINGS, "agent_catalog_dirty", cache=False)
					),
					1,
				)
		frappe.db.commit()

	def test_settings_validate_detects_each_flip(self):
		settings = frappe.get_single(SETTINGS)
		settings.load_doc_before_save()
		settings.enforce_agent_min_model = 1
		self.assertEqual(settings._agent_min_model_flip(), "on")
		settings.enforce_agent_min_model = 0
		self.assertIsNone(settings._agent_min_model_flip())
		_flag(True)
		settings = frappe.get_single(SETTINGS)
		settings.load_doc_before_save()
		self.assertIsNone(settings._agent_min_model_flip())  # already on: no transition
		settings.enforce_agent_min_model = 0
		self.assertEqual(settings._agent_min_model_flip(), "off")

	def test_reconcile_pending_llm_sync_checks_the_pool_and_survives_its_failure(self):
		from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings

		stop = RuntimeError("stop after the hook")
		for hook_effect in (None, RuntimeError("agent model check broke")):
			with (
				patch("jarvis.chat.llm_switch.reconcile"),
				patch.object(agent_models, "check_pool_fingerprint", side_effect=hook_effect) as hook,
				patch.object(jarvis_settings.frappe, "get_single", side_effect=stop) as get_single,
				patch.object(jarvis_settings.frappe, "log_error"),
			):
				jarvis_settings.reconcile_pending_llm_sync()
			hook.assert_called_once()
			get_single.assert_called_once()  # the LLM reconcile still ran after the hook
