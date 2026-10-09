"""Chat failure classification, using the same ordered rules as all chat UIs.

Rules are deliberately conservative: unrecognized text retains the legacy
``gateway`` code, whose copy no longer claims a transient or local cause.

Matching is ASCII-only (``re.ASCII``) on purpose: the JavaScript side runs the
same patterns without the ``u`` flag, so ``\\b`` and case folding must agree on
both engines or a reload could classify a turn differently from the live event.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The JS side slices to the same length before matching.
MAX_CLASSIFY_CHARS = 8192
# A rules file that parses but lost most of its entries would silently turn
# every failure into ``gateway``; refuse to load fewer than this.
MIN_RULES = 20

# A JSON literal exported as an ES module works with Frappe's esbuild 0.14
# and Node without JSON import attributes. Parse data only; never execute JS.
_RULES_PATH = Path(__file__).parents[1] / "public/js/turn_error_rules.mjs"
# Anchored on the export and the closing "];" so a stray trailing comment or
# a reworded prefix fails loudly here (import time) with a clear message,
# instead of raising IndexError inside the turn's own error-reporting path.
_RULE_LITERAL = re.compile(r"^export default\s*(\[.*\])\s*;?\s*$", re.DOTALL | re.MULTILINE)


class RulesFileError(RuntimeError):
	"""The shared rules file could not be turned into a usable rule table."""


def _read_rule_data(path: Path) -> list[dict]:
	"""The raw rule objects of ``turn_error_rules.mjs``, in order."""
	match = _RULE_LITERAL.search(path.read_text(encoding="utf-8"))
	if not match:
		raise RulesFileError(f"{path.name}: expected 'export default [ ... ];' holding a JSON array")
	try:
		return json.loads(match.group(1))
	except json.JSONDecodeError as exc:
		raise RulesFileError(f"{path.name}: rules literal is not valid JSON: {exc}") from exc


def _load_rules(path: Path) -> list[tuple[str, re.Pattern, str]]:
	"""Parse ``turn_error_rules.mjs`` into ``[(code, compiled_pattern, headline), ...]``.

	Every malformed case names the offending rule, so a bad edit is caught at
	deploy time with a message that says what to fix.
	"""
	raw_rules = _read_rule_data(path)
	rules: list[tuple[str, re.Pattern, str]] = []
	for index, rule in enumerate(raw_rules):
		code = rule.get("code") if isinstance(rule, dict) else None
		pattern = rule.get("pattern") if isinstance(rule, dict) else None
		if not code or pattern is None:
			raise RulesFileError(f"{path.name}: rule #{index} needs both 'code' and 'pattern'")
		try:
			rules.append(
				(code, re.compile(pattern, re.IGNORECASE | re.ASCII), str(rule.get("headline") or ""))
			)
		except re.error as exc:
			raise RulesFileError(f"{path.name}: rule {code!r} has an invalid pattern: {exc}") from exc
	codes = {code for code, _, _ in rules}
	if len(rules) < MIN_RULES or "gateway" not in codes or "internal" not in codes:
		raise RulesFileError(
			f"{path.name}: only {len(rules)} rules loaded; expected at least {MIN_RULES} incl. gateway/internal"
		)
	logger.info("chat error taxonomy: loaded %d rules from %s", len(rules), path.name)
	return rules


_RULES = _load_rules(_RULES_PATH)

# Chat-subscription sign-ins. The labels and upstream patterns are the
# subscription-expired rule's own data, read from the same file as turn_errors.mjs;
# only the short algorithm below is mirrored (fixtures/subscription_upstreams.json pins both).
_FLAGS = re.IGNORECASE | re.ASCII
_SUBSCRIPTION = next(rule for rule in _read_rule_data(_RULES_PATH) if rule["code"] == "subscription-expired")[
	"upstreams"
]
SUBSCRIPTION_LABELS: dict[str, str] = _SUBSCRIPTION["labels"]
_PROVIDER_TOKEN_IDS: dict[str, str] = _SUBSCRIPTION["providerTokens"]
_PROVIDERS_TOKEN = re.compile(r"providers=([a-z]+)", _FLAGS)
_SIGNAL = re.compile(_SUBSCRIPTION["signal"], _FLAGS)
_MENTIONS = tuple((upstream, re.compile(p, _FLAGS)) for upstream, p in _SUBSCRIPTION["mentions"].items())
_IMPLIED_OPENAI = re.compile(r"refresh_token_", _FLAGS)
_IMPLIED_ANTHROPIC = re.compile(r"/login|\bnot logged in\b", _FLAGS)


def _text_of(raw) -> str:
	return (json.dumps(raw, default=str) if isinstance(raw, (dict, list)) else str(raw or ""))[
		:MAX_CLASSIFY_CHARS
	]


def subscription_upstream_from_error(raw) -> str:
	"""Which chat-subscription upstream an error text is about (``openai`` / ``anthropic`` /
	``xai`` / ``kimi``), or ``""`` when it cannot be told. Same algorithm as
	``subscriptionUpstreamFromError`` in turn_errors.mjs: a ``providers=<id>`` token wins, else
	the upstream named nearest BEFORE the first sign-in-dead signal, else the first one after
	it, else the upstream the signal itself implies."""
	text = _text_of(raw)
	named = _PROVIDERS_TOKEN.search(text)
	if named and named.group(1).lower() in _PROVIDER_TOKEN_IDS:
		return _PROVIDER_TOKEN_IDS[named.group(1).lower()]
	signal = _SIGNAL.search(text)
	signal_at = signal.start() if signal else len(text)
	before = after = None
	for upstream, pattern in _MENTIONS:
		for match in pattern.finditer(text):
			if match.start() < signal_at:
				if before is None or match.start() > before[1]:
					before = (upstream, match.start())
			elif after is None or match.start() < after[1]:
				after = (upstream, match.start())
	nearest = before or after
	if nearest:
		return nearest[0]
	if _IMPLIED_OPENAI.search(text):
		return "openai"
	if _IMPLIED_ANTHROPIC.search(text):
		return "anthropic"
	return ""


def _fill_provider(headline: str, text: str) -> str:
	"""Replace the ``{provider}`` placeholder; with no upstream to name, drop it entirely."""
	if "{provider}" not in headline:
		return headline
	label = SUBSCRIPTION_LABELS.get(subscription_upstream_from_error(text))
	return headline.replace("{provider}", label) if label else "Sign-in expired"


def classify_error_text(raw) -> str:
	"""Return the first matching rule code for ``raw`` (any shape), else ``gateway``."""
	text = _text_of(raw)
	return next((code for code, pattern, _ in _RULES if pattern.search(text)), "gateway")


def headline_for(raw) -> str | None:
	"""The user-facing headline of the first rule that matches ``raw``, or None when
	no rule does. The same text the chat shows above a failed turn, for callers that
	must say why something failed away from the chat (a macro run row)."""
	text = _text_of(raw)
	for _, pattern, headline in _RULES:
		if headline and pattern.search(text):
			return _fill_provider(headline, text)
	return None
