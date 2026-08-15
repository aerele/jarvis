"""TDD test for admin_client.post_update_llm_pool.

Verifies that post_update_llm_pool POSTs to the update_llm_pool endpoint
with the correct body {spec, api_keys, oauth_blobs} using the shared _post
helper (the same authenticated transport as post_update_llm_creds).
"""

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client


class TestAdminClientPool(FrappeTestCase):
	def test_post_update_llm_pool_posts_to_endpoint(self):
		# _post is the shared authenticated helper used by post_update_llm_creds
		# and all other authenticated endpoints in admin_client.
		with patch.object(admin_client, "_post") as mock_post:
			mock_post.return_value = {"ok": True}
			result = admin_client.post_update_llm_pool(
				spec={"name": "t", "routing_mode": "dynamic", "models": []},
				api_keys={"K": "v"},
				oauth_blobs={},
			)
			self.assertEqual(result, {"ok": True})
			mock_post.assert_called_once()
			args, kwargs = mock_post.call_args
			# path must contain update_llm_pool
			path = args[0] if args else kwargs.get("path", "")
			self.assertIn("update_llm_pool", path)
			# body must be passed as positional or keyword
			body = args[1] if len(args) > 1 else kwargs.get("body", {})
			self.assertEqual(body["spec"]["routing_mode"], "dynamic")
			self.assertEqual(body["api_keys"], {"K": "v"})
			self.assertEqual(body["oauth_blobs"], {})

	def test_post_update_llm_pool_keyword_only_args(self):
		"""Verify that post_update_llm_pool enforces keyword-only arguments (*)."""
		with patch.object(admin_client, "_post") as mock_post:
			mock_post.return_value = {}
			# Keyword-only: calling with positional args should raise TypeError
			with self.assertRaises(TypeError):
				admin_client.post_update_llm_pool(
					{"name": "t", "routing_mode": "dynamic", "models": []},
					{"K": "v"},
					{},
				)
