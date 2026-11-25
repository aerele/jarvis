"""`SnapshotSource`: the `ReportSource` interface over a report snapshot (plan B.2).

`read()` never reloads: a miss here deletes nothing it did not read and asks the caller
to send the call again; `_report_snapshots.ensure_live` is the one place a reload starts.
"""

import hashlib
import zlib

import frappe
import redis

from jarvis.exceptions import (
	DelegateReportRefused,
	InvalidArgumentError,
	PermissionDeniedError,
	RetryLaterError,
)
from jarvis.tools import _delegate_capability
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools._report_query_data import CONTRACT, MAX_COLUMNS, MAX_EXPANDED, encode
from jarvis.tools._report_query_source import (
	MAX_RECEIPT_BYTES,
	_canonical,
	_column_policy,
	_db_budget,
	gate_report,
	receipt_filters,
	scope_digest,
)
from jarvis.tools._report_snapshot_digest import perm_digest
from jarvis.tools._report_snapshot_events import (
	CHANGED,
	NO_ACCESS,
	NOT_AVAILABLE,
	PINNED_MISMATCH,
	not_available,
	read_event,
	redis_down,
)
from jarvis.tools._report_snapshot_payload import MAX_GZIP


def _inflate(content):
	"""The JSON bytes of a snapshot's gzip, or None when it is not a complete bounded gzip."""
	try:
		decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
		raw = decoder.decompress(content, MAX_EXPANDED + 1)
	except zlib.error:
		return None
	if len(raw) > MAX_EXPANDED or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
		return None
	return raw


class SnapshotSource:
	"""The `ReportSource` interface over a snapshot. `read()` never reloads."""

	def __init__(self, snapshot_id, version=None):
		if not store.valid_id(snapshot_id) or not store.enabled():
			raise InvalidArgumentError(NOT_AVAILABLE)
		self.name = snapshot_id
		self.user = frappe.session.user
		self.expected_version = version or None
		self.version = None
		self.context = None
		self.schema = None
		self.meta = None
		self.report = None
		self.filter_defs = []

	def _load_meta(self):
		try:
			return store.load_meta(self.name)
		except redis.exceptions.RedisError as exc:
			raise redis_down(self.name, exc) from None

	def _changed(self, delete):
		if delete:
			store.delete_content(self.name, self.meta)
		read_event("changed", self.name, self.meta)
		return RetryLaterError(CHANGED)

	def read(self):
		meta = self._load_meta()
		if meta is None or meta["owner"] != self.user:
			raise not_available(self.name)
		self.meta = meta
		digest = perm_digest(self.user, meta["report_name"])
		try:
			content = store.load_content(self.name)
		except redis.exceptions.RedisError as exc:
			raise redis_down(self.name, exc) from None
		if content is None:
			raise self._changed(delete=False)
		if digest != meta["perm_digest"]:
			raise self._changed(delete=True)
		self._gate()
		raw = _inflate(content) if len(content) <= MAX_GZIP else None
		if raw is None or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
			raise self._changed(delete=True)
		return content, {"legacy_columns": [], "add_total_row": False}

	def _gate(self):
		"""The gate of a prepared report (`gate_report`) plus the delegate refusal that
		`run_report` applies, so a live read is refused as a reload would be."""
		refusal = _delegate_capability.snapshot_config_report_refusal(self.meta["report_name"])
		if refusal:
			read_event("refused", self.name, self.meta, reason="delegate")
			raise DelegateReportRefused(refusal)
		try:
			self.report, self.filter_defs = gate_report(
				self.meta["report_name"], self.meta["filters"], self.user
			)
		except (frappe.PermissionError, PermissionDeniedError):
			read_event("refused", self.name, self.meta, reason="permission")
			raise PermissionDeniedError(NO_ACCESS) from None
		except (frappe.ValidationError, InvalidArgumentError):
			raise not_available(self.name, self.meta) from None

	def authorize(self, schema, deadline):
		with _db_budget(deadline):
			return self._authorize(schema, deadline)

	def _authorize(self, schema, deadline):
		if (
			not isinstance(schema, dict)
			or not isinstance(schema.get("columns"), list)
			or len(schema["columns"]) > MAX_COLUMNS
		):
			raise InvalidArgumentError("Report worker returned an invalid schema.")
		self.schema = schema
		deadline.check()
		ref_meta = frappe.get_meta(self.report.ref_doctype)
		masked, denied = _column_policy(ref_meta, schema["columns"], self.user, self.report.name)
		self.context = {
			"masked_columns": sorted(masked),
			"denied_columns": sorted(denied),
			"proof_fields": [],
		}
		self.version = hashlib.sha256(
			_canonical(
				[
					CONTRACT,
					frappe.local.site,
					self.user,
					self.name,
					self.meta["sha256"],
					schema,
					self.context["masked_columns"],
					self.context["denied_columns"],
					self.meta["perm_digest"],
				]
			).encode()
		).hexdigest()
		if self.expected_version is not None and self.expected_version != self.version:
			read_event("refused", self.name, self.meta, reason="version")
			text = PINNED_MISMATCH
			if self.meta["generation"] > 0:
				text += f" The snapshot was reloaded at {self.meta.get('reloaded_at')}; inspect it again."
			raise InvalidArgumentError(text)
		return self.context

	def validate(self, schema, context, proofs, deadline):
		rows = proofs.get("rows") if isinstance(proofs, dict) else None
		if (
			not isinstance(rows, list)
			or proofs.get("fields") != context["proof_fields"]
			or any(not isinstance(values, list) or values for values in rows)
		):
			raise InvalidArgumentError("Report worker returned invalid access proofs.")
		deadline.check()
		self.confirm()

	def confirm(self):
		"""The last check before rows leave: the meta record is the one `read()` saw, and the
		permission digest, recomputed now (not the memo of the call), is still the stored
		one. It fences the meta record and the digest inputs, not the rows."""
		current = self._load_meta()
		if current is None or any(
			current.get(key) != self.meta.get(key) for key in ("owner", "sha256", "generation", "perm_digest")
		):
			raise self._changed(delete=False)
		if (
			current["owner"] != self.user
			or perm_digest(self.user, current["report_name"], fresh=True) != current["perm_digest"]
		):
			raise self._changed(delete=False)

	def receipt(self, sql_executed=False):
		meta = self.meta
		result = {
			"prepared_report": self.name,
			"report_name": meta["report_name"],
			"as_of": meta.get("reloaded_at") or meta.get("created_at"),
			"created_at": meta.get("created_at"),
			"generation": meta["generation"],
			"filters": receipt_filters(meta["filters"], self.context),
			"scope_digest": scope_digest(meta["filters"]),
			"version": self.version,
			"sql_executed": bool(sql_executed),
		}
		if len(encode(result)) > MAX_RECEIPT_BYTES:
			raise InvalidArgumentError("This report snapshot's filters are too large. Run run_report again.")
		return result

	def record_live(self, sql_executed):
		read_event("live", self.name, self.meta, sql_executed)
