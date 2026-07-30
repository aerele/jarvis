"""The test suite does not talk to the network.

Why
---
Running this suite against a bench that has a real tenant DESTROYS it. On 2026-07-14,
``bench --site site.jarvis run-tests --app jarvis`` wiped a live tenant's LLM pool and an
OAuth subscription credential that could not be recovered.

The mechanism: ``Jarvis Settings.on_update`` runs the admin sync INLINE under
``frappe.flags.in_test`` -- deliberately, "so tests see the final status without polling"
(``_enqueue_pool_sync``). Test helpers call ``settings.save()`` + ``frappe.db.commit()``,
and the commit defeats ``FrappeTestCase``'s rollback, so the FIXTURE pool persists in the
real Jarvis Settings and is pushed to a live fleet-agent -- which rewrites the tenant's
bifrost config, tears down cliproxy via ``--remove-orphans``, and deletes the OAuth token
blob. Frappe keeps no ``Version`` history for a Single, so there is no undo.

It also explains a ``models=[] with proxy_active=1`` state that was misdiagnosed for hours
as an ``on_update``/``db_set`` ordering race. It was never a race. It was a test run.

CI never caught it because **CI is safe by ACCIDENT** -- nothing answers there, so the call
fails and the test sees an error status. On a developer's bench, jarvis.admin and the
fleet-agent ARE running, so the identical call SUCCEEDS.

Where the block belongs
-----------------------
At the TRANSPORT -- the exact layer a mock replaces. This matters, and getting it wrong is
easy: a guard placed in ``admin_client._do_post`` (say) fires even when the test has
already patched ``requests.post``, so it breaks tests that were never unsafe. Dozens of
existing tests patch the transport and then call the function under test precisely to
exercise its own logic -- ``test_chat_agent_client`` calls ``AgentSession.connect``
directly, because ``connect`` IS the unit under test.

So the rule is: block only what actually reaches the wire.

  * patched ``requests.post``               -> never reaches HTTPAdapter.send -> untouched
  * patched ``websocket.create_connection`` -> replaced outright              -> untouched
  * NOTHING patched                         -> hits the real transport        -> BLOCKED

Each block raises the SAME exception a dead server raises (``requests.ConnectionError``,
``ConnectionRefusedError``), so every caller's existing error handling applies unchanged
and a local run behaves exactly as CI already does. A novel exception type would have
quietly changed test outcomes across the suite.

Escape hatch: ``JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1`` for a deliberate e2e run.
"""

import os

_ALLOW_ENV = "JARVIS_ALLOW_REAL_NETWORK_IN_TESTS"

_MSG = (
	"BLOCKED: this test tried to open a REAL network connection to {target!r}.\n"
	"\n"
	"The test suite must not reach a live admin, fleet-agent, agent container, or "
	"upstream provider. On a developer's bench those are RUNNING, and a test that reaches "
	"them pushes its fixtures into a real tenant -- rewriting its LLM pool and deleting "
	"OAuth credentials, with no undo. That is not hypothetical; it happened.\n"
	"\n"
	"Mock the transport your code uses (requests.post / websocket.create_connection / "
	"urlopen). If you genuinely mean to hit the network, run with " + _ALLOW_ENV + "=1."
)


def _install_network_block() -> None:
	if os.environ.get(_ALLOW_ENV):
		return

	# --- requests: every requests.* call funnels through the adapter's send() ----------
	try:
		import requests
		from requests.adapters import HTTPAdapter

		def _blocked_send(self, request, *a, **kw):
			raise requests.ConnectionError(_MSG.format(target=request.url))

		HTTPAdapter.send = _blocked_send
	except Exception:  # pragma: no cover - never break collection over the guard itself
		pass

	# --- websocket: agent's gateway socket -----------------------------------------
	try:
		import websocket

		def _blocked_ws(url, *a, **kw):
			raise ConnectionRefusedError(_MSG.format(target=url))

		websocket.create_connection = _blocked_ws
	except Exception:  # pragma: no cover
		pass

	# --- urllib3: link_fetch pins the resolved IP and drives a connection pool directly,
	# so it never passes through requests at all.
	try:
		from urllib3.connectionpool import HTTPConnectionPool

		def _blocked_urlopen(self, method, url, *a, **kw):
			raise ConnectionRefusedError(_MSG.format(target=f"{self.host}:{self.port}{url}"))

		HTTPConnectionPool.urlopen = _blocked_urlopen
	except Exception:  # pragma: no cover
		pass


_install_network_block()
