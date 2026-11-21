"""Media from a detached tool (runtime 2026.11): the first run ends with a normal final and the
runtime appends the finished image to the session transcript later. A background job polls for
it and seeds it once. The 2026.9.3 yield path keeps harvesting inline and starts no job."""

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
	def _start(self, rows, **kw):
		args = dict(media_rels=None, media_urls=None, yield_continuation=False)
		args.update(kw)
		with (
			patch.object(turn_handler, "_async_media_tool_rows", return_value=rows),
			patch.object(turn_handler.frappe, "enqueue") as enq,
		):
			turn_handler._maybe_start_async_media_harvest("M", "C", "u@x", "run", START_MS, **args)
		return enq

	def test_a_normal_final_after_a_started_media_tool_enqueues_one_deduped_job(self):
		enq = self._start([{"name": "T1", "tool_status": "running"}])
		enq.assert_called_once()
		kw = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.turn_handler.persist_async_media")
		self.assertEqual(
			(kw["queue"], kw["job_id"], kw["deduplicate"]), ("long", "jarvis-async-media::M", True)
		)
		self.assertEqual(kw["timeout"], turn_handler.ASYNC_MEDIA_POLL_TIMEOUT_S + 60)

	def test_no_media_tool_no_job(self):
		self._start([]).assert_not_called()

	def test_the_yield_path_and_media_already_on_the_final_start_no_job(self):
		rows = [{"name": "T1", "tool_status": "running"}]
		self._start(rows, yield_continuation=True).assert_not_called()
		self._start(rows, media_urls=[{"url": URL}]).assert_not_called()
		self._start(rows, media_rels=["/x"]).assert_not_called()

	def test_failed_tool_rows_do_not_count(self):
		am = frappe._dict(seq=5, creation="2026-11-21 10:00:00")
		rows = [
			{"name": "T1", "tool_status": "error", "tool_name": "image_generate"},
			{"name": "T2", "tool_status": "running", "tool_name": "image_generate"},
		]
		with (
			patch.object(turn_handler.frappe.db, "get_value", return_value=am),
			patch.object(turn_handler.frappe, "get_all", return_value=rows) as get_all,
		):
			out = turn_handler._async_media_tool_rows("M", "C")
		self.assertEqual([r["name"] for r in out], ["T2"])
		self.assertEqual(get_all.call_args.kwargs["filters"]["role"], "tool")


class TestJob(unittest.TestCase):
	def _run(self, histories, rows=None, timeout_s=0.01):
		rows = (
			rows
			if rows is not None
			else [{"name": "T1", "tool_status": "running", "tool_name": "image_generate"}]
		)
		pool = _Pool(histories)
		with (
			patch("jarvis.chat.agent_scope.assert_fefo_message_artifact_scope"),
			patch.object(turn_handler, "_async_media_tool_rows", return_value=rows),
			patch.object(turn_handler, "ASYNC_MEDIA_POLL_TIMEOUT_S", timeout_s),
			patch("jarvis.chat.agent_session_pool.checkout", pool.checkout),
			patch.object(turn_handler.frappe.db, "get_value", return_value="jarvis-chat-u-1"),
			patch.object(turn_handler.frappe.db, "set_value") as set_value,
			patch.object(turn_handler.frappe, "get_single", return_value=frappe._dict(agent_url="http://gw")),
			patch.object(turn_handler.time, "sleep"),
			patch("jarvis.chat.generated_media.detect_media_paths", return_value=[]),
			patch.object(turn_handler, "_seed_gateway_media") as seed,
			patch.object(turn_handler, "_publish_to_user") as publish,
		):
			turn_handler.persist_async_media("M", "C", "u@x", "run", START_MS)
		return seed, set_value, publish

	def test_the_image_is_seeded_once_and_the_tool_row_is_closed(self):
		seed, set_value, publish = self._run([_history()])
		seed.assert_called_once()
		self.assertEqual(seed.call_args.args[5], [{"url": URL, "mime_type": "image/png"}])
		self.assertEqual(set_value.call_args.args[2]["tool_status"], "completed")
		self.assertEqual(publish.call_args.args[1]["kind"], "tool:end")

	def test_a_second_run_over_the_same_history_goes_through_the_deduping_seeder(self):
		# The seeder dedups on the attachment id; the job's part is to hand it the same url.
		for _ in range(2):
			seed, _, _ = self._run([_history()])
			self.assertEqual(seed.call_args.args[5][0]["url"], URL)

	def test_no_image_by_the_bound_seeds_nothing_but_still_closes_the_row(self):
		seed, set_value, _ = self._run([_history(with_image=False)], timeout_s=0)
		seed.assert_not_called()
		self.assertEqual(set_value.call_args.args[2]["tool_status"], "completed")

	def test_no_media_tool_row_means_no_polling(self):
		seed, set_value, _ = self._run([_history()], rows=[])
		seed.assert_not_called()
		set_value.assert_not_called()


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
