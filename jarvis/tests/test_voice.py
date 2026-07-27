"""Tests for jarvis.chat.voice (STT config resolution + transcribe endpoint)
and jarvis.admin_client.get_stt_config.

All HTTP is mocked (requests.post is patched); every test runs on a bare
site with no network and no admin onboarding.
"""

import contextlib
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jarvis import admin_client
from jarvis.chat import voice

TEST_KEY = "test-openrouter-key-123"
WEBSITE_USER = "voice-portal-user@test.invalid"


def _conf(**overrides):
	"""Temporarily override site_config keys (restored on exit)."""
	return patch.dict(frappe.local.conf, overrides)


def _no_admin_stt():
	"""Managed-path stub: admin has no STT config (also guarantees no network)."""
	return patch("jarvis.admin_client.get_stt_config", return_value=None)


def _response(status_code=200, json_body=None, text=""):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status_code
	if json_body is None:
		resp.json.side_effect = ValueError("no json")
		resp.text = text
	else:
		resp.json.return_value = json_body
		resp.text = text
	return resp


def _ok_response(text="hello world"):
	"""The transcription endpoint's success shape: {"text", "usage"}."""
	return _response(200, {"text": text, "usage": {"seconds": 3}})


def _ok_completion(content="hello world"):
	"""The chat-completions success shape (openrouter_complete's text callers)."""
	return _response(200, {"choices": [{"message": {"content": content}}]})


class _FakeUpload:
	"""Just enough of werkzeug's FileStorage for transcribe_audio."""

	def __init__(self, data: bytes, content_type: str = "audio/webm", filename: str | None = "clip.webm"):
		self._data = data
		self.content_type = content_type
		self.filename = filename

	def read(self) -> bytes:
		return self._data


class _FakeRequest:
	def __init__(self, files: dict):
		self.files = files


@contextlib.contextmanager
def _audio_request(
	data=b"\x1aEfake-webm-bytes", content_type="audio/webm", duration_s="5", filename="clip.webm"
):
	"""Fake frappe.request with an ``audio`` upload + form duration_s."""
	req = _FakeRequest({"audio": _FakeUpload(data, content_type, filename)})
	prior_form = frappe.local.form_dict
	frappe.local.form_dict = frappe._dict({"duration_s": duration_s})
	try:
		with patch.object(frappe, "request", req, create=True):
			yield
	finally:
		frappe.local.form_dict = prior_form


class TestSttConfig(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_site_config_wins_over_admin(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model="test/model-x"):
			with patch("jarvis.admin_client.get_stt_config") as mock_admin:
				cfg = voice.stt_config()
		self.assertEqual(cfg, {"enabled": True, "api_key": TEST_KEY, "model": "test/model-x"})
		mock_admin.assert_not_called()

	def test_site_config_default_model(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model=""):
			cfg = voice.stt_config()
		self.assertEqual(cfg["model"], voice._DEFAULT_STT_MODEL)

	def test_site_config_disabled_flag(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_enabled=0):
			self.assertIsNone(voice.stt_config())

	def test_admin_fallback(self):
		with _conf(jarvis_stt_openrouter_api_key=""):
			with patch(
				"jarvis.admin_client.get_stt_config",
				return_value={"enabled": True, "api_key": "admin-key", "model": ""},
			):
				cfg = voice.stt_config()
		self.assertEqual(cfg["api_key"], "admin-key")
		self.assertEqual(cfg["model"], voice._DEFAULT_STT_MODEL)

	def test_admin_disabled_returns_none(self):
		with _conf(jarvis_stt_openrouter_api_key=""):
			with patch(
				"jarvis.admin_client.get_stt_config",
				return_value={"enabled": False, "api_key": "admin-key", "model": "m"},
			):
				self.assertIsNone(voice.stt_config())

	def test_no_config_anywhere_returns_none(self):
		with _conf(jarvis_stt_openrouter_api_key=""), _no_admin_stt():
			self.assertIsNone(voice.stt_config())

	def test_voice_features_off_disables(self):
		frappe.db.set_single_value("Jarvis Settings", "voice_features_enabled", 0, update_modified=False)
		try:
			with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
				self.assertIsNone(voice.stt_config())
		finally:
			frappe.db.set_single_value("Jarvis Settings", "voice_features_enabled", 1, update_modified=False)

	def test_voice_features_absent_row_defaults_on(self):
		"""NULL=ON: a genuinely-absent tabSingles row reads enabled; an
		explicit 0 reads off (row-existence probe, not get_single_value)."""
		rows = frappe.db.sql(
			"select value from tabSingles where doctype='Jarvis Settings' and field='voice_features_enabled'"
		)
		try:
			frappe.db.sql(
				"delete from tabSingles where doctype='Jarvis Settings' and field='voice_features_enabled'"
			)
			self.assertTrue(voice._voice_features_enabled())
			frappe.db.set_single_value("Jarvis Settings", "voice_features_enabled", 0, update_modified=False)
			self.assertFalse(voice._voice_features_enabled())
		finally:
			frappe.db.sql(
				"delete from tabSingles where doctype='Jarvis Settings' and field='voice_features_enabled'"
			)
			if rows:
				frappe.db.set_single_value(
					"Jarvis Settings",
					"voice_features_enabled",
					cint(rows[0][0]),
					update_modified=False,
				)

	def test_chat_ui_settings_carries_stt_enabled(self):
		from jarvis.chat.api import get_chat_ui_settings

		with patch(
			"jarvis.chat.voice.stt_config",
			return_value={"enabled": True, "api_key": "k", "model": "m"},
		):
			self.assertTrue(get_chat_ui_settings()["stt_enabled"])
		with patch("jarvis.chat.voice.stt_config", return_value=None):
			self.assertFalse(get_chat_ui_settings()["stt_enabled"])


class TestUploadPartShape(FrappeTestCase):
	"""The multipart part carries the clip's OWN filename + mime, so a browser
	recording ogg or mp4 (Safari) needs no server-side format mapping table."""

	def test_filename_passed_through(self):
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="audio.m4a")), "audio.m4a")
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="audio.ogg")), "audio.ogg")

	def test_filename_falls_back_when_missing(self):
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="")), "audio.webm")
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename=None)), "audio.webm")

	def test_filename_sanitised_for_the_header(self):
		"""Client-supplied: quotes, CRLF and path segments must not be able to
		reframe the multipart Content-Disposition header."""
		self.assertEqual(
			voice._upload_filename(_FakeUpload(b"x", filename='../../ev"il\r\nname.webm')),
			"ev_il__name.webm",
		)
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="C:\\tmp\\clip.wav")), "clip.wav")
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="....")), "audio.webm")

	def test_filename_length_capped(self):
		self.assertEqual(voice._upload_filename(_FakeUpload(b"x", filename="a" * 300 + ".webm")), "a" * 80)

	def test_mime_parameters_stripped(self):
		self.assertEqual(voice._upload_mime(_FakeUpload(b"x", "audio/webm;codecs=opus")), "audio/webm")
		self.assertEqual(voice._upload_mime(_FakeUpload(b"x", "AUDIO/MP4")), "audio/mp4")
		self.assertEqual(voice._upload_mime(_FakeUpload(b"x", "audio/ogg")), "audio/ogg")

	def test_mime_falls_back_when_missing(self):
		self.assertEqual(voice._upload_mime(_FakeUpload(b"x", "")), "application/octet-stream")
		self.assertEqual(voice._upload_mime(_FakeUpload(b"x", None)), "application/octet-stream")


class TestTextModelDecoupledFromStt(FrappeTestCase):
	"""openrouter_complete's TEXT callers (wiki ingest, voice facts, chat
	mining, wiki lint, insight drafts, trigger LLM actions) all pass no model,
	so they inherit _credentials()'s default. That default must never be the
	resolved STT model: it is transcription-only and 400s on a completion."""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_default_text_model_is_not_the_stt_model(self):
		self.assertNotEqual(voice._DEFAULT_TEXT_MODEL, voice._DEFAULT_STT_MODEL)

	def test_site_stt_model_does_not_leak_into_completions(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model=voice._DEFAULT_STT_MODEL):
			key, model = voice._credentials()
		self.assertEqual(key, TEST_KEY)
		self.assertEqual(model, voice._DEFAULT_TEXT_MODEL)

	def test_admin_stt_model_does_not_leak_into_completions(self):
		with _conf(jarvis_stt_openrouter_api_key=""):
			with patch(
				"jarvis.admin_client.get_stt_config",
				return_value={"enabled": True, "api_key": "admin-key", "model": voice._DEFAULT_STT_MODEL},
			):
				key, model = voice._credentials()
		self.assertEqual(key, "admin-key")
		self.assertEqual(model, voice._DEFAULT_TEXT_MODEL)

	def test_site_text_model_override(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_text_model="vendor/chat-model"):
			_key, model = voice._credentials()
		self.assertEqual(model, "vendor/chat-model")

	def test_completion_posts_text_model_to_chat_endpoint(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model=voice._DEFAULT_STT_MODEL):
			with patch("jarvis.chat.voice.requests.post", return_value=_ok_completion("ok")) as mock_post:
				out = voice.openrouter_complete([{"role": "user", "content": "hi"}])
		self.assertEqual(out, "ok")
		self.assertEqual(mock_post.call_args.args[0], voice._OPENROUTER_URL)
		self.assertEqual(mock_post.call_args.kwargs["json"]["model"], voice._DEFAULT_TEXT_MODEL)


class TestTranscribeAudio(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", WEBSITE_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": WEBSITE_USER,
					"first_name": "Voice Portal",
					"user_type": "Website User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("User", WEBSITE_USER, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_guest_rejected(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			voice.transcribe_audio()

	def test_website_user_rejected(self):
		frappe.set_user(WEBSITE_USER)
		with self.assertRaises(frappe.PermissionError):
			voice.transcribe_audio()

	def test_no_config_rejected(self):
		with _conf(jarvis_stt_openrouter_api_key=""), _no_admin_stt():
			with _audio_request():
				with self.assertRaises(frappe.ValidationError):
					voice.transcribe_audio()

	def test_oversize_rejected(self):
		big = b"x" * (15 * 1024 * 1024 + 1)
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request(data=big):
				with patch("jarvis.chat.voice.requests.post") as mock_post:
					with self.assertRaises(frappe.ValidationError):
						voice.transcribe_audio()
		mock_post.assert_not_called()

	def test_overlong_duration_rejected(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request(duration_s="301"):
				with patch("jarvis.chat.voice.requests.post") as mock_post:
					with self.assertRaises(frappe.ValidationError):
						voice.transcribe_audio()
		mock_post.assert_not_called()

	def test_happy_path_posts_multipart_to_transcription_endpoint(self):
		data = b"\x1aEfake-webm-bytes"
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model="test/model-x"):
			with _audio_request(data=data, content_type="audio/webm;codecs=opus", duration_s="7"):
				with patch(
					"jarvis.chat.voice.requests.post",
					return_value=_ok_response("  hello world \n"),
				) as mock_post:
					out = voice.transcribe_audio()

		self.assertTrue(out["ok"])
		self.assertEqual(out["text"], "hello world")
		self.assertEqual(out["model"], "test/model-x")
		self.assertIsInstance(out["stt_ms"], int)

		self.assertEqual(mock_post.call_args.args[0], voice._OPENROUTER_TRANSCRIBE_URL)
		kwargs = mock_post.call_args.kwargs
		self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {TEST_KEY}")
		# requests must own the multipart Content-Type: it carries the boundary.
		self.assertNotIn("Content-Type", kwargs["headers"])
		self.assertEqual(kwargs["timeout"], (voice._CONNECT_TIMEOUT_S, voice._TRANSCRIBE_READ_TIMEOUT_S))
		self.assertEqual(kwargs["data"], {"model": "test/model-x"})
		self.assertNotIn("json", kwargs)
		filename, payload, mime = kwargs["files"]["file"]
		self.assertEqual(filename, "clip.webm")
		self.assertEqual(payload, data)
		self.assertEqual(mime, "audio/webm")

	def test_never_calls_chat_completions(self):
		"""Regression pin for the paraphrase defect: STT rides the transcription
		API only. On chat-completions a chat model 'transcribes' by inventing
		fluent text and returning it as a 200."""
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch("jarvis.chat.voice.requests.post", return_value=_ok_response()) as mock_post:
					voice.transcribe_audio()
		self.assertTrue(mock_post.call_args_list)
		for call in mock_post.call_args_list:
			url = call.args[0] if call.args else call.kwargs.get("url", "")
			self.assertNotEqual(url, voice._OPENROUTER_URL)
			self.assertNotIn("chat/completions", url)
			self.assertNotIn("json", call.kwargs)

	def test_browser_container_forwarded_verbatim(self):
		"""No mime->format table any more: whatever the browser recorded is
		what the endpoint is told (Safari mp4, Firefox ogg, wav, mp3)."""
		cases = (
			("audio/mp4", "audio.m4a"),
			("audio/ogg;codecs=opus", "audio.ogg"),
			("audio/wav", "audio.wav"),
			("audio/mpeg", "audio.mp3"),
		)
		for content_type, filename in cases:
			with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
				with _audio_request(content_type=content_type, filename=filename):
					with patch("jarvis.chat.voice.requests.post", return_value=_ok_response()) as mock_post:
						voice.transcribe_audio()
			sent_name, _payload, sent_mime = mock_post.call_args.kwargs["files"]["file"]
			self.assertEqual(sent_name, filename)
			self.assertEqual(sent_mime, content_type.split(";")[0])

	def test_admin_key_and_model_used_on_the_managed_path(self):
		with _conf(jarvis_stt_openrouter_api_key=""):
			with patch(
				"jarvis.admin_client.get_stt_config",
				return_value={"enabled": True, "api_key": "admin-key", "model": "admin/model"},
			):
				with _audio_request():
					with patch("jarvis.chat.voice.requests.post", return_value=_ok_response()) as mock_post:
						out = voice.transcribe_audio()
		self.assertEqual(out["model"], "admin/model")
		self.assertEqual(mock_post.call_args.kwargs["data"], {"model": "admin/model"})
		self.assertEqual(mock_post.call_args.kwargs["headers"]["Authorization"], "Bearer admin-key")

	def test_default_model_is_a_transcription_model(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY, jarvis_stt_model=""):
			with _audio_request():
				with patch("jarvis.chat.voice.requests.post", return_value=_ok_response()) as mock_post:
					out = voice.transcribe_audio()
		self.assertEqual(voice._DEFAULT_STT_MODEL, "openai/whisper-large-v3-turbo")
		self.assertEqual(out["model"], voice._DEFAULT_STT_MODEL)
		self.assertEqual(mock_post.call_args.kwargs["data"], {"model": voice._DEFAULT_STT_MODEL})

	def test_silent_clip_returns_empty_text_not_an_invention(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch("jarvis.chat.voice.requests.post", return_value=_ok_response("")):
					out = voice.transcribe_audio()
		self.assertTrue(out["ok"])
		self.assertEqual(out["text"], "")

	def test_non_json_response_raises(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch(
					"jarvis.chat.voice.requests.post",
					return_value=_response(200, None, text="<html>gateway</html>"),
				):
					with self.assertRaises(frappe.ValidationError):
						voice.transcribe_audio()

	def test_missing_text_key_raises(self):
		"""An honest error beats a silent empty composer: the endpoint promised
		a transcript and did not deliver one."""
		for body in ({"usage": {"seconds": 1}}, {"text": None}, {"text": 42}, []):
			with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
				with _audio_request():
					with patch("jarvis.chat.voice.requests.post", return_value=_response(200, body)):
						with self.assertRaises(frappe.ValidationError):
							voice.transcribe_audio()

	def test_retry_once_on_timeout(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch(
					"jarvis.chat.voice.requests.post",
					side_effect=[requests.Timeout("boom"), _ok_response("after retry")],
				) as mock_post:
					out = voice.transcribe_audio()
		self.assertEqual(out["text"], "after retry")
		self.assertEqual(mock_post.call_count, 2)

	def test_retry_once_on_5xx(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch(
					"jarvis.chat.voice.requests.post",
					side_effect=[_response(502, text="bad gateway"), _ok_response("recovered")],
				) as mock_post:
					out = voice.transcribe_audio()
		self.assertEqual(out["text"], "recovered")
		self.assertEqual(mock_post.call_count, 2)

	def test_double_timeout_raises(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch(
					"jarvis.chat.voice.requests.post",
					side_effect=[requests.Timeout("a"), requests.Timeout("b")],
				) as mock_post:
					with self.assertRaises(frappe.ValidationError):
						voice.transcribe_audio()
		self.assertEqual(mock_post.call_count, 2)

	def test_4xx_does_not_retry(self):
		with _conf(jarvis_stt_openrouter_api_key=TEST_KEY):
			with _audio_request():
				with patch(
					"jarvis.chat.voice.requests.post",
					return_value=_response(401, {"error": {"message": "bad key"}}),
				) as mock_post:
					with self.assertRaises(frappe.ValidationError):
						voice.transcribe_audio()
		self.assertEqual(mock_post.call_count, 1)


class TestAdminGetSttConfig(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(admin_client._STT_CONFIG_CACHE_KEY)

	def tearDown(self):
		frappe.cache().delete_value(admin_client._STT_CONFIG_CACHE_KEY)

	def test_error_returns_none(self):
		with patch(
			"jarvis.admin_client._post",
			side_effect=admin_client.AdminAuthError("not onboarded"),
		):
			self.assertIsNone(admin_client.get_stt_config())

	def test_error_is_negative_cached(self):
		"""A failed fetch must not make every subsequent call (SPA loads)
		pay a fresh admin round-trip: the miss is cached briefly."""
		with patch(
			"jarvis.admin_client._post",
			side_effect=admin_client.AdminAuthError("not onboarded"),
		) as mock_post:
			self.assertIsNone(admin_client.get_stt_config())
			self.assertIsNone(admin_client.get_stt_config())
		self.assertEqual(mock_post.call_count, 1)

	def test_non_dict_returns_none(self):
		with patch("jarvis.admin_client._post", return_value=None):
			self.assertIsNone(admin_client.get_stt_config())

	def test_uses_short_timeout(self):
		"""Best-effort hot-path fetch: never the 90s default timeout."""
		with patch(
			"jarvis.admin_client._post",
			return_value={"enabled": 1, "api_key": "k1", "model": "m1"},
		) as mock_post:
			admin_client.get_stt_config()
		self.assertEqual(mock_post.call_args.kwargs["timeout_s"], 5)

	def test_success_normalized_and_cached(self):
		with patch(
			"jarvis.admin_client._post",
			return_value={"enabled": 1, "api_key": "k1", "model": "m1"},
		) as mock_post:
			first = admin_client.get_stt_config()
			second = admin_client.get_stt_config()
		self.assertEqual(first, {"enabled": True, "api_key": "k1", "model": "m1"})
		self.assertEqual(second, first)
		self.assertEqual(mock_post.call_count, 1)
