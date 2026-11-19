"""Owner-bound Redis snapshots of cut inline report results, read by `query_report`.

The callers (`api`, `query_report`) use this module only. The work is split by
responsibility:

  _report_snapshot_events   fixed texts, typed errors, `read` events, failure log lines
  _report_snapshot_digest   the permission digest
  _report_snapshot_payload  skip rules, caps and the gzip payload
  _report_snapshot_store    Redis keys, meta, create and reload writes, index, deletes
  _report_snapshot_source   `SnapshotSource`, the `ReportSource` interface over a snapshot
  _report_snapshot_reload   the server reload and its agent-path scope
  this module               `ensure_live`, the one place a reload starts (B.9.1)
"""

import frappe
import redis

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _report_snapshot_reload as reload_module
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools._report_snapshot_digest import perm_digest
from jarvis.tools._report_snapshot_events import (
	NOT_AVAILABLE,
	begin_read,
	log,
	muted_reads,
	not_available,
	read_event,
	record_read_error,
	redis_down,
)
from jarvis.tools._report_snapshot_reload import ReloadInfo, agent_reload_scope, in_agent_reload_scope
from jarvis.tools._report_snapshot_source import SnapshotSource
from jarvis.tools._report_snapshot_store import create, reload_enabled

__all__ = [
	"ReloadInfo",
	"SnapshotSource",
	"agent_reload_scope",
	"begin_read",
	"create",
	"ensure_live",
	"in_agent_reload_scope",
	"log",
	"muted_reads",
	"perm_digest",
	"read_event",
	"record_read_error",
	"reload_enabled",
]


def ensure_live(snapshot_id):
	"""Check a snapshot id before `query_report` starts its deadline (B.9.1).

	Returns None when its rows are live and the permission digest is unchanged. Else a
	reload is needed: it returns a `ReloadInfo` once the snapshot was reloaded, or raises
	the error that says why it was not (`_report_snapshot_reload`)."""
	if not store.enabled() or not store.valid_id(snapshot_id):
		raise InvalidArgumentError(NOT_AVAILABLE)
	try:
		meta, content_pttl = store.read_state(snapshot_id)
	except redis.exceptions.RedisError as exc:
		raise redis_down(snapshot_id, exc) from None
	if meta is None or meta["owner"] != frappe.session.user:
		raise not_available(snapshot_id)
	digest = perm_digest(frappe.session.user, meta["report_name"])
	present = content_pttl != -2
	if present and digest == meta["perm_digest"]:
		return None
	if present:
		# Old rows are never served under a changed digest.
		store.delete_content(snapshot_id, meta)
		miss = "changed"
	else:
		miss = "evicted" if store.was_evicted(meta) else "not_available"
	return reload_module.reload_snapshot(
		snapshot_id, meta, perm_changed=digest != meta["perm_digest"], miss=miss
	)
