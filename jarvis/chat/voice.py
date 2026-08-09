"""Speech-to-text for the chat composer (voice notes / business tab).

Config resolution (``stt_config``) is two-tier: explicit site_config keys win
(dev benches: ``jarvis_stt_openrouter_api_key`` + optional
``jarvis_stt_model`` / ``jarvis_stt_enabled``), else the managed path asks the
admin app via ``jarvis.admin_client.get_stt_config`` (Redis-cached, never
raises). Transcription itself is one OpenRouter TRANSCRIPTION call: the clip is
posted as multipart to ``/audio/transcriptions`` under its own filename and
mime — no bytes are stored on the bench and nothing is written to disk.

Why not chat-completions: a chat model told to "transcribe" paraphrases, and
its failure mode is a fluent HTTP 200 fabrication — on a clean probe clip it
rewrote "seven lazy dogs" to "the lazy dog", and real mic audio degraded into
invented sentences that the assistant then answered. A transcription model on
the transcription API either returns the words or errors; it cannot quietly
invent them. It also retires the mime -> format table: the endpoint reads the
container from the part itself, so a browser recording mp4 (Safari) or ogg
works without a mapping entry.

``openrouter_complete`` (chat-completions) stays for its TEXT callers — wiki
ingest, voice facts, chat mining, wiki lint, insight drafts, trigger LLM
actions — and is deliberately gated only on "a key is resolvable", not on the
enabled flags: those paths must keep working when e.g. mic capture is switched
off but wiki stays on. Its default model is ``_DEFAULT_TEXT_MODEL`` and NOT the
resolved STT model, which is transcription-only and cannot serve a completion.
"""

import base64
import re
import time

import frappe
import requests
from frappe import _
from frappe.utils import cint

# Reuse the battle-tested redaction from the admin boundary so a provider
# error echoing our Authorization header can never reach the UI / Error Log.
from jarvis.admin_client import _scrub_secrets
from jarvis.permissions import require_jarvis_user

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_TRANSCRIBE_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
_CONNECT_TIMEOUT_S = 10
_TRANSCRIBE_READ_TIMEOUT_S = 60

# Matches Jarvis Admin Settings.stt_model_id's default; used whenever neither
# site config nor admin names a model. The container path standardises on the
# same model.
_DEFAULT_STT_MODEL = "openai/whisper-large-v3-turbo"

# Chat-completions default for openrouter_complete's text callers, overridable
# per site with ``jarvis_text_model``. Deliberately NOT the STT model: the two
# shared one constant while STT rode chat-completions, and reusing it now would
# hand a transcription-only model to every wiki / mining / trigger completion.
_DEFAULT_TEXT_MODEL = "google/gemini-2.5-flash-lite"

_MAX_AUDIO_BYTES = 15 * 1024 * 1024
_MAX_DURATION_S = 300

# V3 review: the byte cap alone permits far more AUDIO than the duration cap intends (15 MB at a
# browser's default ~129 kbps is ~16 minutes), and ``duration_s`` is client-asserted, so neither
# bounds spend on its own. Cross-check them: a recording may not carry more bytes than a bitrate
# ceiling allows for the length it claims. The ceiling is deliberately loose — our recorders pin
# 32 kbps, but a browser still running a previously-cached bundle encodes at its own default
# (~129 kbps measured on Chrome) and must NOT be rejected. 160 kbps keeps every real encoder
# comfortably inside while cutting a 300 s claim's allowance from 15 MB to ~6.5 MB; the fixed
# headroom covers container overhead and a short recording's initialisation segment.
_MAX_AUDIO_BITRATE_BPS = 160_000
_AUDIO_SIZE_HEADROOM_BYTES = 256 * 1024

# The clip filename is client-supplied and lands in a multipart
# Content-Disposition header: keep it to characters that cannot reframe it.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_FALLBACK_AUDIO_FILENAME = "audio.webm"

# The clip's Content-Type is client-supplied too and is written VERBATIM into the outbound
# multipart part header. Stripping surrounding whitespace is not enough — anything that is not a
# plain ``type/subtype`` is replaced rather than forwarded, so no embedded control character can
# reframe the part or inject a field into the provider request.
_MEDIA_TYPE_RE = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+")
_FALLBACK_AUDIO_MIME = "application/octet-stream"

# ONE transcription attempt. whisper-turbo does 300 s of audio in 1.2 s, so the retry bought
# almost nothing while doubling the server's worst case (2 x (10 s connect + 60 s read) = 140 s)
# — and the client's own budget has to cover that PLUS the upload, which requests' timeout does
# not bound. The client still retries once itself, after a backoff.
_TRANSCRIBE_ATTEMPTS = 1

# --- chat-audio (Gemini via Bifrost) mode ---------------------------------- #
_DEFAULT_STT_MODE = "chat-audio"
_STT_MODE_CHAT_AUDIO = "chat-audio"
_STT_MODE_TRANSCRIPTION = "transcription"
# Default chat-audio model; overridable per tenant. Pin the exact id the spike proved.
_DEFAULT_CHAT_AUDIO_MODEL = "google/gemini-2.5-flash-lite"
# Base64 inflates bytes ~33%; keep the chat-audio cap below Gemini's inline-data ceiling.
# Confirmed against the deployed image in Task 0.4; real dictation clips are ~1.2 MB.
_CHAT_AUDIO_MAX_BYTES = 8 * 1024 * 1024
# Gemini audio is slower than whisper-turbo; the client still owns retries so keep ONE
# server attempt but give it a longer read budget (measured in Task 0.2).
_CHAT_AUDIO_READ_TIMEOUT_S = 120
_STT_TRANSLATE_SYSTEM = (
	"You are a transcription engine. Transcribe the audio and translate it to English. "
	"Output only the spoken words in English, nothing else. Do not answer, explain, or add text."
)
_AUDIO_FORMAT_BY_MIME = {
	"audio/webm": "webm",
	"audio/ogg": "ogg",
	"video/mp4": "mp4",
	"audio/mp4": "mp4",
	"audio/wav": "wav",
	"audio/x-wav": "wav",
	"audio/mpeg": "mp3",
}


def _voice_features_enabled() -> bool:
	"""Operator toggle; NULL-safe (a pre-existing config without the field
	defaults to ON), mirroring vision_attachments_enabled. Probes tabSingles
	row-existence directly: get_single_value (like a loaded Document) coerces
	an unset Check field to 0, which would break the NULL=ON idiom."""
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		("Jarvis Settings", "voice_features_enabled"),
	)
	if not row:
		return True
	return bool(cint(row[0][0]))


def _site_config_key() -> str:
	return (frappe.conf.get("jarvis_stt_openrouter_api_key") or "").strip()


def _credentials() -> tuple[str, str]:
	"""Resolve ``(api_key, chat_model)`` for the chat-completions callers,
	ignoring the enabled flags — the wiki / voice-facts extraction callers need
	the key even when mic capture is off. Returns ``("", <default model>)``
	when no key is resolvable anywhere.

	The model is the TEXT default (site ``jarvis_text_model`` when set), never
	the configured STT model: that one is a transcription-only model now and
	would be rejected by /chat/completions.
	"""
	model = (frappe.conf.get("jarvis_text_model") or "").strip() or _DEFAULT_TEXT_MODEL
	key = _site_config_key()
	if key:
		return key, model
	from jarvis import admin_client

	cfg = admin_client.get_stt_config() or {}
	if cfg.get("api_key"):
		return cfg["api_key"], model
	return "", model


def stt_config() -> dict | None:
	"""Resolved speech-to-text config ``{"enabled", "api_key", "model"}`` or
	None when voice features / STT are off or no key is available.

	Site config WINS when ``jarvis_stt_openrouter_api_key`` is present
	(dev benches); the managed path defers to admin's tenant config
	(Redis-cached in admin_client, errors degrade to None).
	"""
	if not _voice_features_enabled():
		return None
	key = _site_config_key()
	if key:
		enabled = frappe.conf.get("jarvis_stt_enabled")
		# NULL=ON: a bench that set only the key clearly wants STT.
		if enabled is not None and not cint(enabled):
			return None
		model = (frappe.conf.get("jarvis_stt_model") or "").strip()
		return {"enabled": True, "api_key": key, "model": model or _DEFAULT_STT_MODEL}
	from jarvis import admin_client

	cfg = admin_client.get_stt_config()
	if not cfg or not cfg.get("enabled") or not cfg.get("api_key"):
		return None
	model = (cfg.get("model") or "").strip()
	return {"enabled": True, "api_key": cfg["api_key"], "model": model or _DEFAULT_STT_MODEL}


# Why STT is unavailable — the UI treats these differently, so collapsing them into one
# boolean (as ``bool(stt_config())`` does) is not enough. Mirrors the support states.
STT_OK = "ok"
STT_OFF = "off"  # an admin deliberately turned voice features off
STT_UNCONFIGURED = "unconfigured"  # no key anywhere — an admin can still set it up
STT_ERROR = "error"  # transient: the CP lookup blew up; self-heals


def stt_state() -> str:
	"""Which of the FOUR states STT is in, not merely whether it works.

	``stt_config()`` returns None for three very different reasons, and a UI that says
	"not set up" for all of them lies twice: it tells an admin who deliberately disabled
	voice to go ask an admin, and it turns a transient CP blip into a permanent-looking
	misconfiguration. Only ``unconfigured`` is an actionable gap worth surfacing.

	Deliberately does NOT re-implement stt_config's resolution — it asks the same
	questions in the same order and defers to it for the positive answer, so the two
	cannot drift."""
	if not _voice_features_enabled():
		return STT_OFF
	try:
		cfg = stt_config()
	except Exception:
		return STT_ERROR
	if cfg:
		return STT_OK
	# Voice is on and nothing raised, so the only remaining reasons are "no key" or an
	# explicit per-bench stt_enabled=0 — both "an operator has not set this up".
	return STT_UNCONFIGURED


def openrouter_complete(
	messages: list,
	model: str | None = None,
	max_tokens: int = 2000,
	temperature: float = 0,
	timeout: int = 60,
) -> str:
	"""One OpenRouter chat-completions call; returns the assistant text.

	One retry on timeout / 5xx (transient upstream); 4xx never retries.
	Raises ``frappe.ValidationError`` with a secret-scrubbed message on any
	failure — callers (transcribe, wiki ingest, voice facts) surface it as-is.
	"""
	key, default_model = _credentials()
	if not key:
		frappe.throw(_("Speech-to-text is not configured on this site."), frappe.ValidationError)
	payload = {
		"model": model or default_model,
		"messages": messages,
		"max_tokens": int(max_tokens),
		"temperature": temperature,
	}
	headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
	last_error = ""
	for _attempt in range(2):
		try:
			resp = requests.post(
				_OPENROUTER_URL,
				json=payload,
				headers=headers,
				timeout=(_CONNECT_TIMEOUT_S, timeout),
			)
		except requests.Timeout:
			last_error = "request timed out"
			continue
		except requests.RequestException as e:
			frappe.throw(
				_("OpenRouter request failed: {0}").format(_scrub_secrets(str(e))),
				frappe.ValidationError,
			)
		if resp.status_code >= 500:
			last_error = f"upstream error {resp.status_code}"
			continue
		if resp.status_code != 200:
			detail = ""
			try:
				err = resp.json().get("error")
				detail = err.get("message") if isinstance(err, dict) else str(err or "")
			except Exception:
				detail = (getattr(resp, "text", "") or "")[:200]
			frappe.throw(
				_("OpenRouter rejected the request ({0}): {1}").format(
					resp.status_code, _scrub_secrets(detail or "no detail")
				),
				frappe.ValidationError,
			)
		try:
			content = resp.json()["choices"][0]["message"]["content"]
		except Exception:
			content = None
		if not isinstance(content, str):
			frappe.throw(
				_("OpenRouter returned an unexpected response shape."),
				frappe.ValidationError,
			)
		return content
	frappe.throw(
		_("OpenRouter request failed after retry: {0}").format(_scrub_secrets(last_error)),
		frappe.ValidationError,
	)


def _upload_filename(upload) -> str:
	"""The clip's own filename, reduced to what is safe in a multipart
	Content-Disposition header (it is client-supplied). Falls back to the
	recorder's default when nothing usable arrives; the endpoint reads the
	container from the part's mime, so the extension is a hint, not a
	contract."""
	raw = (getattr(upload, "filename", None) or "").strip()
	base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
	return _UNSAFE_FILENAME_CHARS.sub("_", base)[:80].strip("._") or _FALLBACK_AUDIO_FILENAME


def _upload_mime(upload) -> str:
	"""The clip's own media type with parameters stripped: the recorder appends
	``;codecs=opus``, which the decoder sniffs from the container anyway and
	which some multipart parsers reject.

	The result goes straight into the outbound multipart part header, so it is
	validated as a whole token, not merely trimmed — a value carrying CR/LF or
	anything else outside ``type/subtype`` is replaced with the generic type
	(the transcription endpoint reads the container from the bytes regardless).
	"""
	mime = (getattr(upload, "content_type", None) or "").split(";")[0].strip().lower()
	if not mime or not _MEDIA_TYPE_RE.fullmatch(mime):
		return _FALLBACK_AUDIO_MIME
	return mime


def _openrouter_transcribe(content: bytes, filename: str, mime: str, model: str, api_key: str) -> str:
	"""One OpenRouter transcription call; returns the transcript text.

	Same transport contract as ``openrouter_complete`` (4xx never retries,
	secret-scrubbed messages), but the request is multipart ``file`` + ``model``
	against the transcription endpoint and it does NOT retry
	(``_TRANSCRIBE_ATTEMPTS``): the client owns the retry, and a second server
	attempt only doubles the budget the caller has to wait out. Anything other
	than a 200 carrying a JSON ``text`` raises: a transcript this function
	cannot read out of the provider is an error, never a plausible string
	handed to the composer as if it were speech.
	"""
	headers = {"Authorization": f"Bearer {api_key}"}
	last_error = ""
	for _attempt in range(_TRANSCRIBE_ATTEMPTS):
		try:
			resp = requests.post(
				_OPENROUTER_TRANSCRIBE_URL,
				files={"file": (filename, content, mime)},
				data={"model": model},
				headers=headers,
				timeout=(_CONNECT_TIMEOUT_S, _TRANSCRIBE_READ_TIMEOUT_S),
			)
		except requests.Timeout:
			last_error = "request timed out"
			continue
		except requests.RequestException as e:
			frappe.throw(
				_("OpenRouter request failed: {0}").format(_scrub_secrets(str(e))),
				frappe.ValidationError,
			)
		if resp.status_code >= 500:
			last_error = f"upstream error {resp.status_code}"
			continue
		if resp.status_code != 200:
			detail = ""
			try:
				err = resp.json().get("error")
				detail = err.get("message") if isinstance(err, dict) else str(err or "")
			except Exception:
				detail = (getattr(resp, "text", "") or "")[:200]
			frappe.throw(
				_("OpenRouter rejected the transcription ({0}): {1}").format(
					resp.status_code, _scrub_secrets(detail or "no detail")
				),
				frappe.ValidationError,
			)
		try:
			body = resp.json()
		except Exception:
			frappe.throw(
				_("OpenRouter returned a non-JSON transcription response."),
				frappe.ValidationError,
			)
		text = body.get("text") if isinstance(body, dict) else None
		if not isinstance(text, str):
			frappe.throw(
				_("OpenRouter returned no transcript for this recording."),
				frappe.ValidationError,
			)
		return text
	frappe.throw(
		_("OpenRouter transcription failed: {0}").format(_scrub_secrets(last_error)),
		frappe.ValidationError,
	)


def _audio_format_token(mime: str) -> str:
	"""Map a media type to Gemini's ``format`` token for an ``input_audio`` part.
	Unknown types fall back to ``webm``, our recorder's own default."""
	return _AUDIO_FORMAT_BY_MIME.get((mime or "").split(";")[0].strip().lower(), "webm")


def _bifrost_chat_audio_transcribe(
	content: bytes, mime: str, model: str, api_key: str, base_url: str
) -> str:
	"""One Bifrost ``/chat/completions`` call carrying the clip as an
	``input_audio`` part; returns the English text. Same transport discipline
	as ``_openrouter_transcribe`` (no retry here, the client owns it; 4xx never
	retries; every non-200 or unreadable body raises a scrubbed error rather
	than handing a plausible fabrication to the composer)."""
	url = base_url.rstrip("/") + "/chat/completions"
	payload = {
		"model": model,
		"temperature": 0,
		"messages": [
			{"role": "system", "content": _STT_TRANSLATE_SYSTEM},
			{
				"role": "user",
				"content": [
					{
						"type": "input_audio",
						"input_audio": {
							"data": base64.b64encode(content).decode("ascii"),
							"format": _audio_format_token(mime),
						},
					}
				],
			},
		],
	}
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	try:
		resp = requests.post(
			url,
			json=payload,
			headers=headers,
			timeout=(_CONNECT_TIMEOUT_S, _CHAT_AUDIO_READ_TIMEOUT_S),
		)
	except requests.RequestException as e:
		frappe.throw(_("Voice request failed: {0}").format(_scrub_secrets(str(e))), frappe.ValidationError)
	if resp.status_code != 200:
		detail = ""
		try:
			err = resp.json().get("error")
			detail = err.get("message") if isinstance(err, dict) else str(err or "")
		except Exception:
			detail = (getattr(resp, "text", "") or "")[:200]
		frappe.throw(
			_("Voice gateway rejected the request ({0}): {1}").format(
				resp.status_code, _scrub_secrets(detail or "no detail")
			),
			frappe.ValidationError,
		)
	try:
		content_out = resp.json()["choices"][0]["message"]["content"]
	except Exception:
		content_out = None
	if not isinstance(content_out, str):
		frappe.throw(_("Voice gateway returned no transcript for this recording."), frappe.ValidationError)
	return content_out


@frappe.whitelist()
@require_jarvis_user
def transcribe_audio() -> dict:
	"""Transcribe one recorded clip (multipart field ``audio`` + form
	``duration_s``). Desk (System User) only; bytes are size/duration capped
	and streamed straight to OpenRouter's transcription endpoint — never
	persisted on the bench.

	Returns ``{"ok": True, "text", "stt_ms", "model"}``.
	"""
	t0 = time.monotonic()
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("You must be signed in to transcribe audio."), frappe.PermissionError)
	if frappe.db.get_value("User", user, "user_type") != "System User":
		frappe.throw(_("Only desk users can transcribe audio."), frappe.PermissionError)
	cfg = stt_config()
	if not cfg:
		frappe.throw(_("Speech-to-text is not enabled on this site."), frappe.ValidationError)

	files = getattr(frappe.request, "files", None) or {}
	upload = files.get("audio")
	if upload is None:
		frappe.throw(_("No audio uploaded (multipart field 'audio' is required)."), frappe.ValidationError)
	content = upload.read()
	if not content:
		frappe.throw(_("The uploaded audio is empty."), frappe.ValidationError)
	if len(content) > _MAX_AUDIO_BYTES:
		frappe.throw(_("Audio is too large (max 15 MB)."), frappe.ValidationError)
	duration_s = cint(frappe.form_dict.get("duration_s") or 0)
	if duration_s > _MAX_DURATION_S:
		frappe.throw(_("Recording is too long (max 5 minutes)."), frappe.ValidationError)
	# Bytes cross-checked against the claimed length (see _MAX_AUDIO_BITRATE_BPS). An unstated
	# duration is charged at the full cap, so omitting the field buys nothing.
	claimed_s = duration_s if duration_s > 0 else _MAX_DURATION_S
	if len(content) > claimed_s * (_MAX_AUDIO_BITRATE_BPS // 8) + _AUDIO_SIZE_HEADROOM_BYTES:
		frappe.throw(
			_("Audio is larger than a {0}-second recording can be.").format(claimed_s),
			frappe.ValidationError,
		)

	t_stt = time.monotonic()
	text = _openrouter_transcribe(
		content,
		_upload_filename(upload),
		_upload_mime(upload),
		cfg["model"],
		cfg["api_key"],
	)
	stt_ms = int((time.monotonic() - t_stt) * 1000)

	from jarvis.chat.latency import get_logger

	get_logger().info(
		"transcribe user=%s model=%s bytes=%d stt_ms=%d total_ms=%d",
		user,
		cfg["model"],
		len(content),
		stt_ms,
		int((time.monotonic() - t0) * 1000),
	)
	return {"ok": True, "text": (text or "").strip(), "stt_ms": stt_ms, "model": cfg["model"]}
