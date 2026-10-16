"""Owner-only transcript downloads. Markdown is the source for both formats."""

from __future__ import annotations

import contextlib
import html
import json
import re
import time
import unicodedata
from contextlib import ExitStack
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import get_url, md_to_html, now_datetime

from jarvis._redis_lock import redis_lock
from jarvis.chat.api import _get_owned_conversation, _live_turn_steps
from jarvis.chat.error_taxonomy import headline_for
from jarvis.chat.scrub import scrub_for_share
from jarvis.permissions import require_jarvis_access
from jarvis.tools._export.document import furniture
from jarvis.tools._export.document.sanitizer import sanitize_rich
from jarvis.tools._export.document.theme import component_css

MAX_PDF_CHARS = 200_000
PDF_TIMEOUT = 60
_RATE_COUNTER = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class _ExportRenderError(frappe.ValidationError, frappe.SecurityException):
	"""Safe failure: prevent Frappe snapshots from collecting transcript locals."""


BLOCK_TAGS = frozenset(
	{
		"jarvis-action",
		"confirm",
		"jarvis-ask",
		"jarvis-cards",
		"jarvis-skill",
		"jarvis-macro",
		"jarvis-chart",
		"jarvis-goto",
	}
)
CHART_TYPES = frozenset(
	{
		"bar",
		"line",
		"area",
		"pie",
		"donut",
		"scatter",
		"bubble",
		"heatmap",
		"boxplot",
		"radar",
		"funnel",
		"gauge",
	}
)
_FIELDS = [
	"name",  # Internal only: the existing streaming-step projection needs row identity.
	"origin",
	"via_voice",
	"role",
	"content",
	"creation",
	"error",
	"stopped",
	"streaming",
	"tool_name",
	"tool_status",
	"action_outcome",
	"canvas",
	"ref_doctype",
	"ref_name",
]
_FENCE = re.compile(r"```(" + "|".join(sorted(BLOCK_TAGS | {"mermaid"})) + r")[ \t]*\n([\s\S]*?)(?:```|\Z)")
_URL_WORD = re.compile(r"""[^\s<>"'()\[\]]+""")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~\-])")


def _safe_text(value) -> str:
	def encoded_url(match):
		word = match[0]
		decoded = word
		if any(char in word for char in ("&", "%", "\\")):
			for _ in range(3):
				decoded = html.unescape(unquote(decoded)).replace("\\/", "/")
		if "/private/files/" in decoded.lower():
			return "[Private attachment]"
		if word.startswith("/app/"):
			return get_url(word)
		return word

	return scrub_for_share(_URL_WORD.sub(encoded_url, "" if value is None else str(value)))


def _escape(value) -> str:
	return _MARKDOWN.sub(r"\\\1", html.escape(_safe_text(value), quote=False))


def _cell(value) -> str:
	if isinstance(value, (dict, list)):
		value = json.dumps(value, ensure_ascii=False)
	return _escape(value).replace("\n", " ").replace("\r", " ")


def _table(headers: list, rows: list) -> str:
	def line(cells):
		return "| " + " | ".join(_cell(cell) for cell in cells) + " |"

	return "\n".join(
		[line(headers), "| " + " | ".join("---" for _ in headers) + " |"] + [line(row) for row in rows]
	)


def _json(text, fallback=None):
	try:
		return json.loads(text) if isinstance(text, str) else text
	except (ValueError, TypeError, RecursionError):
		return fallback


def _xychart(body: str) -> dict | None:
	if not re.match(r"\s*xychart-beta\b", body):
		return None

	def split(values):
		return [
			re.sub(r"""^["']|["']$""", "", next(v for v in match.groups() if v is not None).strip())
			for match in re.finditer(r""""([^"]*)"|'([^']*)'|([^,]+)""", values)
		]

	title = re.search(r'title[ \t]+"([^"]*)"', body)
	y = re.search(r'y-axis[ \t]+"([^"]*)"', body)
	x = re.search(r"x-axis[ \t]+\[([^\]]*)\]", body)
	series = []
	bar = False
	for match in re.finditer(r'\b(bar|line)[ \t]+(?:"([^"]*)"[ \t]+)?\[([^\]]*)\]', body):
		data = []
		for value in split(match[3]):
			try:
				number = float(value)
				data.append(int(number) if number.is_integer() else number)
			except (ValueError, OverflowError):
				continue
		if data:
			bar = bar or match[1] == "bar"
			series.append({"name": match[2] or (y[1] if y else "Value"), "data": data})
	return {
		"title": title[1] if title else "Chart",
		"type": "bar" if bar else "line",
		"x": split(x[1]) if x else [],
		"series": series,
	}


def _render_chart(body: str, mermaid: bool = False) -> str:
	spec = _xychart(body) if mermaid else _json(body)
	title = spec.get("title") or "Chart" if isinstance(spec, dict) else "Chart"
	fallback = "[Chart: " + _escape(title) + "]"
	if not isinstance(spec, dict) or spec.get("type") not in CHART_TYPES:
		return fallback
	series = spec.get("series")
	if not isinstance(series, list) or not series or not all(isinstance(s, dict) for s in series):
		return fallback
	x = spec.get("x")
	data = spec.get("data")
	# Same records-form normalization as chartTheme.js; leave the stored spec alone.
	if isinstance(data, list) and isinstance(x, str) and all(isinstance(r, dict) for r in data):
		x = [r.get(x, "") for r in data]
		series = [
			{**s, "data": [r.get(s["field"], 0) for r in data]}
			if s.get("field") and not isinstance(s.get("data"), list)
			else s
			for s in series
		]
	if not any(isinstance(s.get("data"), list) and s["data"] for s in series):
		return fallback
	if not all(isinstance(s.get("data"), list) for s in series):
		return fallback
	if not isinstance(x, list):
		x = []
	if spec["type"] in {"scatter", "bubble"}:
		# Value-axis charts store x in each point, not in spec.x. Group repeated
		# x values without dropping any y values or bubble magnitudes.
		xs = {}
		groups = []
		for s in series:
			group = {}
			for point in s.get("data") or []:
				if not isinstance(point, list) or len(point) < 2:
					return fallback
				key = json.dumps(point[0])
				xs.setdefault(key, point[0])
				group.setdefault(key, []).append(point[1] if len(point) == 2 else point[1:])
			groups.append(group)
		rows = []
		for key, label in xs.items():
			cells = [group.get(key, []) for group in groups]
			rows.append([label] + [cell[0] if len(cell) == 1 else cell for cell in cells])
		headers = ["x"] + [s.get("name") or "Value" for s in series]
	elif spec["type"] == "heatmap":
		# The grid stores [xIndex, yIndex, value] triples; every y value for an
		# x belongs in that row's series cell, rather than indexing the triples.
		ys = spec.get("y") or []
		if not isinstance(ys, list):
			return fallback
		groups = []
		for s in series:
			group = {}
			for point in s.get("data") or []:
				if not isinstance(point, list) or len(point) != 3:
					return fallback
				xi, yi, value = point
				if (
					not isinstance(xi, int)
					or not isinstance(yi, int)
					or not 0 <= xi < len(x)
					or not 0 <= yi < len(ys)
				):
					return fallback
				group.setdefault(xi, []).append([ys[yi], value])
			groups.append(group)
		rows = [[label] + [group.get(i, []) for group in groups] for i, label in enumerate(x)]
		headers = ["x"] + [s.get("name") or "Value" for s in series]
	elif spec["type"] in {"pie", "donut", "gauge"}:
		values = series[0].get("data") or []
		if not isinstance(values, list):
			return fallback
		if spec["type"] == "gauge":
			rows = [[series[0].get("name") or (x[0] if x else "Value"), values[0]]] if values else []
		else:
			rows = [[x[i] if i < len(x) else f"#{i + 1}", value] for i, value in enumerate(values)]
		headers = ["Label", "Value"]
	else:
		headers = ["x"] + [s.get("name") or "Value" for s in series]
		rows = [
			[label]
			+ [s["data"][i] if isinstance(s.get("data"), list) and i < len(s["data"]) else "" for s in series]
			for i, label in enumerate(x)
		]
	if not rows:
		return fallback
	return "### " + _escape(title) + "\n\n" + _table(headers, rows)


def _render_blocks(text: str) -> str:
	def block(match):
		tag, body = match[1], match[2]
		if tag in {"jarvis-action", "confirm", "jarvis-goto"}:
			return ""
		if tag == "jarvis-chart" or (tag == "mermaid" and re.match(r"\s*xychart-beta\b", body)):
			return _render_chart(body, mermaid=tag == "mermaid") + "\n\n"
		if tag == "jarvis-skill":
			names = [
				re.sub(r"^custom-", "", name.strip().lstrip("-* "))
				for name in re.split(r"[\n,]+", body)
				if name.strip()
			]
			return _("Skills used: {0}").format(", ".join(_escape(name) for name in names)) + "\n\n"
		if tag not in {"jarvis-ask", "jarvis-cards", "jarvis-macro"}:
			return match[0]
		parsed = _json(body)
		if tag == "jarvis-macro":
			return (
				_("Saved routine: {0}").format(_escape(parsed.get("name") or "")) + "\n\n"
				if isinstance(parsed, dict)
				else ""
			)
		items = (
			parsed
			if isinstance(parsed, list)
			else parsed.get("questions" if tag == "jarvis-ask" else "cards")
			if isinstance(parsed, dict)
			else None
		)
		if not isinstance(items, list):
			return ""
		if tag == "jarvis-ask":
			return (
				"\n\n".join(
					_escape(q.get("q") or q.get("question") or "") for q in items if isinstance(q, dict)
				)
				+ "\n\n"
			)
		rows = []
		for card in items:
			if not isinstance(card, dict):
				continue
			fields = card.get("fields") or []
			details = (
				"; ".join(
					str(f.get("label") or "") + ": " + str(f.get("value", ""))
					for f in fields
					if isinstance(f, dict)
				)
				if isinstance(fields, list)
				else ""
			)
			rows.append(
				[
					card.get("title") or card.get("name") or "",
					card.get("doctype") or "",
					card.get("name") or "",
					details,
				]
			)
		return _table([_("Title"), _("Type"), _("Name"), _("Details")], rows) + "\n\n" if rows else ""

	return _FENCE.sub(block, text)


def _markdown(conversation, rows: list) -> str:
	owner = frappe.db.get_value("User", conversation.owner, "full_name") or conversation.owner
	agent = frappe.db.get_single_value("Jarvis Settings", "agent_name") or "Jarvis"
	parts = [
		"# " + _escape(conversation.title or "Chat"),
		_("Exported on {0}").format(now_datetime().isoformat(sep=" ", timespec="seconds")),
		_("Owner: {0}").format(_escape(owner)),
		_("Assistant: {0}").format(_escape(agent)),
	]
	if rows:
		parts.append(
			_("Messages: {0} – {1}").format(
				_escape(rows[0].get("creation")), _escape(rows[-1].get("creation"))
			)
		)
	else:
		parts.append(_("No messages yet"))
	tools = []

	def flush_tools():
		if tools:
			parts.append(
				_("Used {0} tools: {1}").format(len(tools), ", ".join(_escape(name) for name in tools))
			)
			tools.clear()

	for row in rows:
		role = row.get("role")
		stamp = str(row.get("creation") or "")
		if role == "user":
			flush_tools()
			parts.append("**" + _escape(owner) + "** · " + _escape(stamp))
			if row.get("origin") in {None, "", "human", "delegated", "board_answer"}:
				# Scrub before adding quote/escape syntax, which would separate a
				# multiline credential assignment or Bearer value from its key.
				content = _safe_text(row.get("content") or "")
				parts.append("\n".join("> " + _escape(line) for line in content.splitlines()))
			else:
				parts.append("_" + _("Automated step") + "_")
		elif role == "tool":
			label = (row.get("tool_name") or "tool").removeprefix("jarvis__")
			tools.append(label)
			outcome = row.get("action_outcome")
			if row.get("tool_status") == "pending":
				parts.append(_("Awaiting confirmation"))
			elif outcome in {"confirmed", "auto_applied"}:
				parts.append(
					_("Created/Updated {0} {1}").format(
						_escape(row.get("ref_doctype")), _escape(row.get("ref_name"))
					)
				)
			elif outcome:
				parts.append(
					_("Discarded")
					if outcome == "discarded"
					else _("Failed")
					if outcome == "failed"
					else _escape(outcome).capitalize() + ": " + _escape(label)
				)
		elif role == "assistant" and (row.get("content") or row.get("error") or row.get("canvas")):
			flush_tools()
			parts.append("**" + _escape(agent) + "** · " + _escape(stamp))
			content = _render_blocks(row.get("content") or "")
			parts.append(_safe_text(content))
			if row.get("streaming"):
				parts.append("(" + _("in progress at export time") + ")")
			if row.get("error"):
				parts.append(_escape(headline_for(row["error"]) or _("Something went wrong")))
			if row.get("stopped"):
				parts.append("(" + _("stopped") + ")")
		canvas = _json(row.get("canvas"), [])
		if isinstance(canvas, list):
			for item in canvas:
				if isinstance(item, dict):
					parts.append(
						"["
						+ _("Attachment: {0} ({1})").format(
							_escape(item.get("title") or "Attachment"), _escape(item.get("type") or "file")
						)
						+ "]"
					)
	flush_tools()
	return _safe_text("\n\n".join(part for part in parts if part)) + "\n"


def _lock_name(name: str) -> str:
	# make_key returns bytes; redis_lock adds its legacy string prefix itself.
	return frappe.cache().make_key(name).decode("utf-8")


def _rate_limit(user: str) -> None:
	cache = frappe.cache()
	key = cache.make_key("jarvis:export:rl:" + user)
	# One Redis transaction: a lost reply/crashed worker cannot orphan an
	# increment without expiry. Also repair counters left by an interrupted call.
	count = cache.eval(_RATE_COUNTER, 1, key, 60)
	if count > 5:
		frappe.throw(_("Too many exports. Try again in a minute."))


def _pdf(conversation, markdown: str) -> bytes:
	started = time.monotonic()
	try:
		with furniture.defer_failure_logging():
			body = sanitize_rich(md_to_html(markdown))
			brand = furniture.resolve_brand(None)
			# The Markdown source already owns the transcript title and metadata.
			masthead = furniture.build_title_block(None, brand=brand)
			return furniture.render_pdf(
				"<style>" + component_css() + "</style>" + masthead + body, timeout=PDF_TIMEOUT
			)
	except Exception:
		# Leave the exception context before logging: Frappe telemetry otherwise
		# captures the original exception instead of the supplied safe message.
		pass
	with contextlib.suppress(Exception):
		frappe.log_error(
			title="Jarvis chat export failed",
			message=json.dumps(
				{
					"conversation": conversation.name,
					"format": "pdf",
					"duration": round(time.monotonic() - started, 3),
				}
			),
			defer_insert=True,
		)
	frappe.throw(_("We couldn't create the PDF. Try Markdown instead."), _ExportRenderError)


@frappe.whitelist(methods=["GET"])
def download_conversation(conversation: str, format: str) -> None:
	user = frappe.session.user
	ok = False
	try:
		require_jarvis_access()
		doc = _get_owned_conversation(conversation)
		if format not in {"pdf", "md"}:
			frappe.throw(_("Unknown format"), frappe.ValidationError)
		_rate_limit(user)
		with ExitStack() as locks:
			if not locks.enter_context(redis_lock(_lock_name("export:" + user), timeout_s=90)):
				frappe.throw(_("An export is already running. Try again in a moment."))
			if format == "pdf":
				for slot in (1, 2):
					if locks.enter_context(redis_lock(_lock_name(f"export:pdf:slot:{slot}"), timeout_s=90)):
						break
				else:
					frappe.throw(_("Too many exports are running. Try again in a moment."))
			rows = frappe.get_all(
				"Jarvis Chat Message",
				filters={"conversation": conversation, "hidden": 0},
				order_by="seq asc",
				fields=_FIELDS,
			)
			_live_turn_steps(rows)
			markdown = _markdown(doc, rows)
			if format == "pdf" and len(markdown) > MAX_PDF_CHARS:
				frappe.throw(_("This chat is too long for a PDF. Download it as Markdown instead."))
			content = _pdf(doc, markdown) if format == "pdf" else markdown.encode("utf-8")
			title = (
				unicodedata.normalize("NFKD", _safe_text(doc.title or "")).encode("ascii", "ignore").decode()
			)
			slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "jarvis-chat"
			frappe.response.update(
				{
					"type": "download",
					"filename": f"{slug}-{now_datetime().date().isoformat()}.{format}",
					"filecontent": content,
					"content_type": "application/pdf" if format == "pdf" else "text/markdown; charset=utf-8",
					"display_content_as": "attachment",
				}
			)
			ok = True
	finally:
		with contextlib.suppress(Exception):
			frappe.logger("jarvis.share_audit").info(
				json.dumps(
					{
						"actor": user,
						"action": "export",
						"conversation": conversation,
						"format": format if format in {"pdf", "md"} else "invalid",
						"ok": ok,
					}
				)
			)
