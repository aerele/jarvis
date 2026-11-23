"""Media from a detached tool (runtime 2026.11): the first run ends with a normal final and the
runtime appends the finished image to the session transcript later. A background job polls for
it and seeds it once. The 2026.9.3 yield path keeps harvesting inline and starts no job."""

import time
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarvis.chat import turn_handler

URL = "/api/chat/media/outgoing/jarvis-chat-u-1/3f2a9c1e-0b7d-4e55-9a11-2c4d5e6f7a80/full"
START_MS = 1_000_000


def _history(*, with_image=True, image_ts=START_MS + 12_000):
	messages = [
		{"role": "user", "timestamp": START_MS - 1000, "content": [{"type": "text", "text": "draw a cat"}]},
		{
			"role": "assistant",
			"timestamp": START_MS + 3000,
			"content": [{"type": "text", "text": "Started. It will arrive shortly as its own message."}],
		},
	]
	if with_image:
		# The 2026.11 shape: empty text content, the image block in the display content.
		messages.append(
			{
				"role": "assistant",
				"timestamp": image_ts,
				"content": [{"type": "image", "url": URL, "mimeType": "image/png"}],
			}
		)
	return {"messages": messages}


class _Pool:
	"""Stands in for agent_session_pool.checkout."""

	def __init__(self, histories):
		self.histories = list(histories)
		self.calls = 0

	def checkout(self, _url):
		pool = self

		class _Ctx:
			def __enter__(self):
				sess = MagicMock()
				idx = min(pool.calls, len(pool.histories) - 1)
				sess.get_history.return_value = pool.histories[idx]
				pool.calls += 1
				return sess

			def __exit__(self, *a):
				return False

		return _Ctx()


def _db_values(field, *a, **k):
	if field == "session_key":
		return "jarvis-chat-u-1"
	return None


class TestHarvestFindsTheNewShape(unittest.TestCase):
	def _harvest(self, histories, timeout_s=0.01):
		pool = _Pool(histories)
		settings = frappe._dict(agent_url="http://gw")
		with (
			patch("jarvis.chat.agent_session_pool.checkout", pool.checkout),
			patch.object(turn_handler.frappe.db, "get_value", return_value="jarvis-chat-u-1"),
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler.time, "sleep"),
			patch("jarvis.chat.generated_media.detect_media_paths", return_value=[]),
		):
			return turn_handler._harvest_post_yield_media("C", START_MS, timeout_s=timeout_s), pool

	def test_an_image_appended_after_the_final_is_found(self):
		(rels, urls), _ = self._harvest([_history()])
		self.assertEqual(urls, [{"url": URL, "mime_type": "image/png"}])

	def test_it_waits_for_a_late_image_and_gives_up_at_the_bound(self):
		(rels, urls), pool = self._harvest([_history(with_image=False), _history()], timeout_s=60)
		self.assertEqual(len(urls), 1)
		self.assertEqual(pool.calls, 2)
		(rels, urls), _ = self._harvest([_history(with_image=False)], timeout_s=0)
		self.assertEqual((rels, urls), ([], []))


class TestYieldPathUnchanged(unittest.TestCase):
	def test_the_inline_yield_harvest_still_uses_its_own_short_bound(self):
		pool = _Pool([_history(with_image=False)])
		with (
			patch("jarvis.chat.agent_session_pool.checkout", pool.checkout),
			patch.object(turn_handler.frappe.db, "get_value", return_value="jarvis-chat-u-1"),
			patch.object(turn_handler.frappe, "get_single", return_value=frappe._dict(agent_url="http://gw")),
			patch.object(turn_handler.time, "sleep"),
			patch.object(turn_handler, "_YIELD_MEDIA_POLL_TIMEOUT_S", 0),
			patch("jarvis.chat.generated_media.detect_media_paths", return_value=[]),
		):
			self.assertEqual(turn_handler._harvest_post_yield_media("C", START_MS), ([], []))
		self.assertEqual(turn_handler._YIELD_MEDIA_POLL_TIMEOUT_S, 20.0)
		self.assertEqual(turn_handler.ASYNC_MEDIA_POLL_TIMEOUT_S, 180.0)


class TestTrigger(unittest.TestCase):
	def _start(self, rows, v11=True, **kw):
		args = dict(media_rels=None, media_urls=None, yield_continuation=False)
		args.update(kw)
		with (
			patch("jarvis.native_runtime.gateway_is_v11", return_value=v11),
			patch.object(turn_handler, "_recent_media_tool_row_exists", return_value=bool(rows)) as cheap,
			patch.object(turn_handler, "_async_media_tool_rows", return_value=rows) as exact,
			patch.object(turn_handler.frappe, "enqueue") as enq,
		):
			turn_handler._maybe_start_async_media_harvest("M", "C", "u@x", "run", START_MS, **args)
		enq.cheap, enq.exact = cheap, exact
		return enq

	def test_a_normal_final_after_a_started_media_tool_enqueues_the_first_slice_on_the_turn_lane(self):
		with patch("jarvis.chat.pump._control_queue", return_value="short"):
			enq = self._start([{"name": "T1", "tool_status": "running"}])
		enq.assert_called_once()
		kw = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.turn_handler.persist_async_media")
		# The lane the turn rides, never `long` (served last on a one-worker bench).
		self.assertEqual(
			(kw["queue"], kw["job_id"], kw["deduplicate"]), ("short", "jarvis-async-media::M::s0", True)
		)
		self.assertEqual(kw["timeout"], turn_handler.ASYNC_MEDIA_JOB_TIMEOUT_S)
		self.assertLess(turn_handler.ASYNC_MEDIA_JOB_TIMEOUT_S, 120)
		self.assertGreater(kw["deadline_ms"], int(time.time() * 1000))

	def test_an_older_runtime_costs_no_query_and_no_job(self):
		enq = self._start([{"name": "T1", "tool_status": "running"}], v11=False)
		enq.assert_not_called()
		enq.cheap.assert_not_called()
		enq.exact.assert_not_called()

	def test_a_turn_with_no_media_tool_row_costs_one_cheap_query_only(self):
		enq = self._start([])
		enq.assert_not_called()
		enq.cheap.assert_called_once()
		enq.exact.assert_not_called()

	def test_no_media_tool_no_job(self):
		self._start([]).assert_not_called()

	def test_the_yield_path_and_media_already_on_the_final_start_no_job(self):
		rows = [{"name": "T1", "tool_status": "running"}]
		self._start(rows, yield_continuation=True).assert_not_called()
		self._start(rows, media_urls=[{"url": URL}]).assert_not_called()
		self._start(rows, media_rels=["/x"]).assert_not_called()

	def _rows(self, rows, next_message=None):
		am = frappe._dict(seq=5, creation="2026-11-21 10:00:00")
		with (
			patch.object(turn_handler.frappe.db, "get_value", return_value=am),
			patch.object(turn_handler.frappe, "get_all", side_effect=[next_message or [], rows]) as get_all,
		):
			out = turn_handler._async_media_tool_rows("M", "C")
		return out, get_all

	def test_only_started_and_not_failed_denied_or_cancelled_rows_count(self):
		rows = [
			{"name": f"T{i}", "tool_status": st, "tool_name": "image_generate"}
			for i, st in enumerate(["error", "denied", "cancelled", "running", "completed", ""])
		]
		out, get_all = self._rows(rows)
		self.assertEqual([r["name"] for r in out], ["T3", "T4", "T5"])
		self.assertEqual(get_all.call_args.kwargs["filters"]["role"], "tool")

	def test_the_next_turns_tool_rows_are_not_this_turns(self):
		# A second turn B starts at seq 8: only rows with seq 6..7 belong to turn A.
		out, get_all = self._rows([], next_message=[{"seq": 8, "creation": "2026-11-21 10:01:00"}])
		self.assertEqual(get_all.call_args.kwargs["filters"]["seq"], ["between", [6, 7]])
		out, get_all = self._rows([])
		self.assertEqual(get_all.call_args.kwargs["filters"]["seq"], [">", 5])

	def test_a_video_or_music_tool_gets_the_longer_bound(self):
		self.assertEqual(turn_handler._async_media_bound_s([{"tool_name": "image_generate"}]), 180.0)
		self.assertEqual(
			turn_handler._async_media_bound_s(
				[{"tool_name": "image_generate"}, {"tool_name": "music_generate"}]
			),
			turn_handler.ASYNC_MEDIA_LONG_POLL_TIMEOUT_S,
		)
		self.assertGreater(turn_handler.ASYNC_MEDIA_LONG_POLL_TIMEOUT_S, 180.0)


class TestAnotherTurnsImageIsNotSeeded(unittest.TestCase):
	def test_the_history_scan_stops_at_the_next_turn(self):
		with patch("jarvis.chat.generated_media.detect_media_paths", return_value=[]):
			url_a = URL
			url_b = URL.replace("3f2a9c1e", "aaaaaaaa")
			messages = [
				{
					"role": "assistant",
					"timestamp": START_MS + 3000,
					"content": [{"type": "text", "text": "A final"}],
				},
				{
					"role": "assistant",
					"timestamp": START_MS + 9000,
					"content": [{"type": "image", "url": url_a, "mimeType": "image/png"}],
				},
				{
					"role": "assistant",
					"timestamp": START_MS + 70_000,
					"content": [{"type": "image", "url": url_b, "mimeType": "image/png"}],
				},
			]
			_, urls = turn_handler._scan_history_for_media(messages, START_MS, until_ms=START_MS + 60_000)
			self.assertEqual([u["url"] for u in urls], [url_a])
			_, urls = turn_handler._scan_history_for_media(messages, START_MS)
			self.assertEqual(len(urls), 2)  # unbounded, as before

	def test_the_window_end_is_the_next_message_time(self):
		am = frappe._dict(seq=5, creation="2026-11-21 10:00:00")
		nxt = [{"seq": 8, "creation": "2026-11-21 10:01:00"}]
		with (
			patch.object(turn_handler.frappe.db, "get_value", return_value=am),
			patch.object(turn_handler.frappe, "get_all", return_value=nxt),
			patch.object(turn_handler.frappe.utils, "get_system_timezone", return_value="UTC"),
		):
			_, next_seq, until_ms = turn_handler._turn_window("M", "C")
		self.assertEqual(next_seq, 8)
		self.assertEqual(until_ms, 1795255260000)  # 2026-11-21 10:01:00 UTC


class TestJob(unittest.TestCase):
	def _run(self, histories, *, have=(), deadline_ms=None, seed_attempts=0, rows=None):
		rows = (
			rows
			if rows is not None
			else [{"name": "T1", "tool_status": "running", "tool_name": "image_generate"}]
		)
		pool = _Pool(histories)
		from jarvis.chat import generated_media

		with (
			patch("jarvis.chat.agent_scope.assert_fefo_message_artifact_scope"),
			patch.object(turn_handler, "_async_media_tool_rows", return_value=rows),
			patch.object(turn_handler, "_turn_window", return_value=(None, None, None)),
			patch.object(turn_handler, "ASYNC_MEDIA_SLICE_S", 0),
			patch("jarvis.chat.agent_session_pool.checkout", pool.checkout),
			patch.object(turn_handler.frappe.db, "get_value", return_value="jarvis-chat-u-1"),
			patch.object(turn_handler.frappe.db, "set_value") as set_value,
			patch.object(turn_handler.frappe, "get_single", return_value=frappe._dict(agent_url="http://gw")),
			patch.object(turn_handler.time, "sleep"),
			patch("jarvis.chat.generated_media.detect_media_paths", return_value=[]),
			patch.object(generated_media, "_existing_media_sources", return_value=set(have)),
			patch.object(turn_handler, "_seed_gateway_media") as seed,
			patch.object(turn_handler, "_enqueue_async_media_slice") as nxt,
			patch.object(turn_handler, "_publish_to_user") as publish,
		):
			turn_handler.persist_async_media(
				"M",
				"C",
				"u@x",
				"run",
				START_MS,
				deadline_ms=deadline_ms or int(time.time() * 1000) + 60_000,
				slice_no=2,
				seed_attempts=seed_attempts,
			)
		return seed, set_value, publish, nxt

	def _have(self):
		from jarvis.chat import generated_media

		return [generated_media._media_url_id(URL)]

	def test_the_image_is_seeded_once_and_the_tool_row_is_closed(self):
		seed, set_value, publish, nxt = self._run([_history()], have=self._have())
		seed.assert_called_once()
		self.assertEqual(seed.call_args.args[5], [{"url": URL, "mime_type": "image/png"}])
		self.assertEqual(set_value.call_args.args[2]["tool_status"], "completed")
		self.assertEqual(publish.call_args.args[1]["kind"], "tool:end")
		nxt.assert_not_called()

	def test_nothing_yet_enqueues_the_next_slice_and_keeps_the_row_open(self):
		seed, set_value, _, nxt = self._run([_history(with_image=False)])
		seed.assert_not_called()
		set_value.assert_not_called()
		self.assertEqual(nxt.call_args.kwargs["slice_no"], 3)

	def test_the_bound_closes_the_row_instead_of_enqueueing_again(self):
		seed, set_value, _, nxt = self._run([_history(with_image=False)], deadline_ms=1)
		nxt.assert_not_called()
		self.assertEqual(set_value.call_args.args[2]["tool_status"], "completed")

	def test_a_failed_fetch_is_retried_in_a_later_slice_but_only_a_few_times(self):
		# The image is in the transcript, but the seeder could not save it (nothing on the canvas).
		seed, set_value, _, nxt = self._run([_history()], have=[])
		seed.assert_called_once()
		self.assertEqual(nxt.call_args.kwargs["seed_attempts"], 1)
		_, set_value, _, nxt = self._run(
			[_history()], have=[], seed_attempts=turn_handler.ASYNC_MEDIA_SEED_ATTEMPTS - 1
		)
		nxt.assert_not_called()
		self.assertEqual(set_value.call_args.args[2]["tool_status"], "completed")

	def test_no_media_tool_row_means_no_polling(self):
		seed, set_value, _, nxt = self._run([_history()], rows=[])
		seed.assert_not_called()
		set_value.assert_not_called()
		nxt.assert_not_called()


class TestFetchAcceptsWhatTheGatewayServes(unittest.TestCase):
	PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32

	def _fetch(self, status=200, ctype="image/png", body=None):
		from jarvis.chat import generated_media

		resp = MagicMock(status_code=status, headers={"Content-Type": ctype})
		resp.iter_content.return_value = [self.PNG if body is None else body]
		resp.__enter__.return_value = resp
		with (
			patch("requests.get", return_value=resp),
			patch("jarvis.chat.canvas._http_base", return_value="http://gw"),
			patch.object(generated_media.frappe, "logger") as logger,
			patch("jarvis.native_runtime.is_native_pool", return_value=False),
			patch("jarvis.native_runtime.gateway_is_v11", return_value=False),
		):
			out = generated_media.fetch_media_url("http://gw", "tok", URL, mime_type="image/png")
		return out, logger

	def test_a_normal_image_response(self):
		self.assertEqual(self._fetch()[0], self.PNG)

	def test_a_generic_binary_type_is_accepted_only_for_real_image_bytes(self):
		self.assertEqual(self._fetch(ctype="application/octet-stream")[0], self.PNG)
		self.assertIsNone(self._fetch(ctype="application/octet-stream", body=b"<html>nope</html>")[0])

	def test_a_failure_says_why_without_the_token(self):
		out, logger = self._fetch(status=404)
		self.assertIsNone(out)
		message = " ".join(str(a) for a in logger.return_value.warning.call_args.args)
		self.assertIn("status 404", message)
		self.assertNotIn("tok", message.replace("attachment", ""))


class TestSeedingIsIdempotent(unittest.TestCase):
	def test_an_attachment_already_seeded_is_not_fetched_or_saved_again(self):
		from jarvis.chat import generated_media

		with (
			patch.object(
				generated_media, "_existing_media_sources", return_value={generated_media._media_url_id(URL)}
			),
			patch.object(generated_media, "fetch_media_url") as fetch,
		):
			out = generated_media.seed_media_urls(
				"M", "http://gw", "tok", [{"url": URL, "mime_type": "image/png"}]
			)
		self.assertEqual(out, [])
		fetch.assert_not_called()


def _resp(status, ctype="image/png", body=b""):
	resp = MagicMock(status_code=status, headers={"Content-Type": ctype})
	resp.iter_content.return_value = [body]
	resp.__enter__.return_value = resp
	return resp


class TestTicketedMediaOnTheNewGateway(unittest.TestCase):
	PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
	TICKET = URL + "?mediaTicket=abc"

	def _fetch(self, responses, ticket=TICKET, mime="image/png"):
		from jarvis.chat import generated_media

		calls = []

		def get(full, headers=None, **kw):
			calls.append((full, dict(headers or {})))
			return responses.pop(0)

		with (
			patch("requests.get", side_effect=get),
			patch("jarvis.chat.canvas._http_base", return_value="http://gw"),
			patch.object(generated_media, "_ticket_path", return_value=ticket) as ticket_path,
			patch.object(generated_media.frappe, "logger"),
			patch("jarvis.native_runtime.is_native_pool", return_value=False),
			patch("jarvis.native_runtime.gateway_is_v11", return_value=False),
		):
			out = generated_media.fetch_media_url("http://gw", "tok", URL, mime_type=mime)
		return out, calls, ticket_path

	def test_a_401_on_the_bearer_falls_back_to_the_ticketed_url_without_a_credential(self):
		out, calls, ticket_path = self._fetch([_resp(401, "application/json"), _resp(200, body=self.PNG)])
		self.assertEqual(out, self.PNG)
		ticket_path.assert_called_once()
		self.assertEqual(calls[0][1], {"Authorization": "Bearer tok"})
		self.assertEqual(calls[1], ("http://gw" + self.TICKET, {}))

	def test_a_gateway_that_accepts_the_bearer_never_asks_for_a_ticket(self):
		out, calls, ticket_path = self._fetch([_resp(200, body=self.PNG)])
		self.assertEqual(out, self.PNG)
		ticket_path.assert_not_called()
		self.assertEqual(len(calls), 1)

	def test_no_ticket_from_the_gateway_means_no_image_and_other_statuses_are_not_retried(self):
		out, calls, _ = self._fetch([_resp(401, "application/json")], ticket=None)
		self.assertIsNone(out)
		out, calls, ticket_path = self._fetch([_resp(404, "text/plain")])
		self.assertIsNone(out)
		ticket_path.assert_not_called()

	def test_the_ticket_request_names_the_artifact_by_family_and_decodes_the_session_key(self):
		from jarvis.chat import agent_session_pool, generated_media
		from jarvis.chat.agent_client import AgentSession

		session = object.__new__(AgentSession)
		asked = []

		attachment = URL.split("/")[-2]
		url = URL.replace("jarvis-chat-u-1", "agent%3Amain%3Adashboard%3Au1")

		def request(method, params, timeout_s=None):
			asked.append((method, params))
			if params["artifactId"].startswith("artifact_managed_media_"):
				raise RuntimeError("not a media artifact")
			return {"payload": {"url": url + "?mediaTicket=t", "expiresAt": "x"}}

		session._request = request

		class _Ctx:
			def __enter__(self_inner):
				return session

			def __exit__(self_inner, *a):
				return False

		with patch.object(agent_session_pool, "checkout", return_value=_Ctx()):
			path = generated_media._ticket_path("http://gw", url, "video/mp4")
		self.assertEqual(path, url + "?mediaTicket=t")
		self.assertEqual(
			[a[1]["artifactId"][:23] for a in asked], ["artifact_managed_media_", "artifact_managed_image_"]
		)
		self.assertEqual(asked[0][0], "artifacts.download")
		self.assertEqual(asked[0][1]["sessionKey"], "agent:main:dashboard:u1")
		self.assertTrue(asked[0][1]["artifactId"].endswith(attachment))

	def test_a_url_that_is_not_a_managed_outgoing_path_gets_no_ticket(self):
		from jarvis.chat import generated_media

		self.assertIsNone(generated_media._ticket_path("http://gw", "/other/path", "image/png"))


class TestTicketHardening(unittest.TestCase):
	KEY = "agent:main:dashboard:u1"
	ATT = "3f2a9c1e-0b7d-4e55-9a11-2c4d5e6f7a80"
	GOOD = f"/api/chat/media/outgoing/agent%3Amain%3Adashboard%3Au1/{ATT}/full?mediaTicket=abc%2F123"

	def _answer(self, url):
		from jarvis.chat.agent_client import AgentSession

		session = object.__new__(AgentSession)
		session._request = lambda *a, **k: {"payload": {"url": url}}
		return session.artifact_download_url(self.KEY, "artifact_managed_image_" + self.ATT)

	def test_only_a_plain_path_for_the_same_session_and_attachment_is_accepted(self):
		self.assertEqual(self._answer(self.GOOD), self.GOOD)
		for bad in (
			"https://evil.example/x",
			"//evil.example/api/chat/media/outgoing/x",
			self.GOOD.replace("full", "../full"),
			self.GOOD.replace("u1", "u2"),
			self.GOOD.replace(self.ATT, self.ATT[:-1] + "1"),
			"/api/other/path",
			None,
		):
			self.assertIsNone(self._answer(bad), bad)

	def test_a_url_for_another_session_is_refused_before_any_request(self):
		from jarvis.chat import agent_session_pool, generated_media

		with patch.object(agent_session_pool, "checkout") as checkout:
			out = generated_media._ticket_path(
				"http://gw", self.GOOD.split("?")[0], "image/png", "agent:main:dashboard:other"
			)
		self.assertIsNone(out)
		checkout.assert_not_called()

	def test_the_time_spent_asking_for_a_ticket_is_capped(self):
		from jarvis.chat import generated_media

		self.assertLessEqual(generated_media._TICKET_BUDGET_S, 15)
		self.assertLess(generated_media._TICKET_RPC_TIMEOUT_S * 2, generated_media._TICKET_BUDGET_S + 1)


class TestFetchQuietAndNoSvg(unittest.TestCase):
	PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32

	def _fetch(self, responses, ticket="/t?mediaTicket=a", mime="image/png", native=False, v11=False):
		from jarvis.chat import generated_media

		with (
			patch("requests.get", side_effect=lambda *a, **k: responses.pop(0)),
			patch("jarvis.chat.canvas._http_base", return_value="http://gw"),
			patch.object(generated_media, "_ticket_path", return_value=ticket),
			patch.object(generated_media.frappe, "logger") as logger,
			patch("jarvis.native_runtime.is_native_pool", return_value=native),
			patch("jarvis.native_runtime.gateway_is_v11", return_value=v11),
		):
			out = generated_media.fetch_media_url("http://gw", "tok", URL, mime_type=mime)
		return out, logger

	def test_the_first_401_is_not_logged_when_the_ticket_fetch_succeeds(self):
		out, logger = self._fetch([_resp(401, "application/json"), _resp(200, body=self.PNG)])
		self.assertEqual(out, self.PNG)
		logger.return_value.warning.assert_not_called()

	def test_the_whole_fetch_failing_is_logged_once(self):
		out, logger = self._fetch([_resp(401, "application/json")], ticket=None)
		self.assertIsNone(out)
		logger.return_value.warning.assert_called_once()

	def test_svg_is_refused_on_a_native_tenant_or_a_2026_11_gateway_but_not_on_an_older_one(self):
		# A deliberate hardening that reaches only those tenants: SVG can carry script.
		svg = _resp(200, "image/svg+xml", b"<svg/>")
		self.assertIsNone(self._fetch([svg], mime="image/svg+xml", native=True)[0])
		svg = _resp(200, "image/svg+xml", b"<svg/>")
		self.assertIsNone(self._fetch([svg], mime="image/svg+xml", v11=True)[0])
		svg = _resp(200, "image/svg+xml", b"<svg/>")
		self.assertEqual(self._fetch([svg], mime="image/svg+xml")[0], b"<svg/>")  # older runtime: as before

	def test_the_ticket_fetch_refuses_svg_for_everyone(self):
		first = _resp(401, "application/json")
		svg = _resp(200, "image/svg+xml", b"<svg/>")
		self.assertIsNone(self._fetch([first, svg])[0])
