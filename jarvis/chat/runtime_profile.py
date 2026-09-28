"""Private, site-local mirror of the Admin transport profile.

A valid Admin-supplied profile is required. Missing or malformed data and
connection mismatches fail explicitly. No network I/O, public API, or
global/user default is involved.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import frappe

PARENT = "__jarvis_runtime_internal"
KEY = "runtime_profile_v1"
ROW = "jarvis-runtime-profile-v1"
CACHE_KEY = "jarvis:runtime_profile:v1"
MAX_BYTES = 16 * 1024
_MEMO = "jarvis_runtime_profile_snapshot"
_ERROR = "Runtime configuration is unavailable. Ask your Jarvis administrator to sync the connection."


class RuntimeProfileError(frappe.ValidationError):
	pass


@dataclass(frozen=True)
class RuntimeProfile:
	message_metadata_key: str
	canvas_route: str
	media_route: str
	live_reload_route: str
	media_root: str


class ConnectionData(dict):
	"""Internal receipt: dict serialization never includes the profile."""

	def __init__(self, data, profile):
		super().__init__(data)
		self.runtime_profile = profile


def private_connection(data):
	if not isinstance(data, dict) or "runtime_profile" not in data:
		return data
	clean = dict(data)
	profile = clean.pop("runtime_profile")
	return ConnectionData(clean, profile)


def _json(value):
	return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate(raw, connection=None):
	"""Validate an authenticated Admin receipt, not authenticate its source.

	The digest proves internal consistency. Image compatibility belongs to
	Admin; local path checks and assignment binding remain mandatory.
	Exception messages never include the rejected values.
	"""
	try:
		if not isinstance(raw, dict) or len(_json(raw).encode()) > MAX_BYTES:
			raise ValueError
		if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
			raise ValueError
		if raw.get("profile_id") != "gateway-v1":
			raise ValueError
		values = raw["values"]
		if set(values) != set(RuntimeProfile.__dataclass_fields__):
			raise ValueError
		if any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in values.values()):
			raise ValueError
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", values["message_metadata_key"]):
			raise ValueError
		for key in ("canvas_route", "media_route", "live_reload_route", "media_root"):
			path = values[key]
			if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
				raise ValueError
			if any(part in ("", ".", "..") for part in path.strip("/").split("/")) or "//" in path:
				raise ValueError
		if not values["canvas_route"].endswith("/") or not values["media_root"].endswith("/"):
			raise ValueError
		profile = RuntimeProfile(**values)
		contract = {k: raw[k] for k in ("schema_version", "profile_id", "values")}
		if raw.get("revision") != hashlib.sha256(_json(contract).encode()).hexdigest():
			raise ValueError
		binding = raw["runtime_binding"]
		if type(binding.get("generation")) is not int or binding["generation"] < 0:
			raise ValueError
		if not re.fullmatch(r"[a-f0-9]{32}", binding.get("handle", "")):
			raise ValueError
		if not isinstance(binding.get("image"), str) or not 1 <= len(binding["image"]) <= 256:
			raise ValueError
		if not isinstance(binding.get("gateway_url"), str) or not binding["gateway_url"]:
			raise ValueError
		if connection is not None and (
			binding["generation"] != int(connection.get("tenant_authority_generation") or 0)
			or binding["handle"] != connection.get("tenant_authority_handle")
			or binding["gateway_url"] != connection.get("agent_url")
		):
			raise ValueError
		return profile
	except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
		raise RuntimeProfileError(_ERROR) from None


def _anchor(settings):
	from jarvis.admin_client import _admin_url

	return {
		"admin_url": _admin_url(settings),
		"customer": settings.get("jarvis_admin_customer_email") or "",
		"agent_url": settings.get("agent_url") or "",
		"tenant_authority_generation": int(settings.get("tenant_authority_generation") or 0),
		"tenant_authority_handle": settings.get("tenant_authority_handle") or "",
	}


def _invalidate():
	try:
		frappe.cache().delete_value(CACHE_KEY)
	except Exception:
		pass  # Durable DB copy remains authoritative; cache entries have a short TTL.


def _forget_snapshot():
	if hasattr(frappe.local, _MEMO):
		delattr(frappe.local, _MEMO)


def persist(raw, settings, *, unavailable=False):
	"""Called inside the serialized connection write, in the same transaction."""
	anchor = _anchor(settings)
	if not unavailable:
		profile = validate(raw, anchor)
	record = {"profile": raw, "anchor": anchor, "fetched_at": datetime.now(UTC).isoformat()}
	if unavailable:
		record["unavailable"] = True
	blob = _json(record)
	if len(blob.encode()) > MAX_BYTES:
		raise RuntimeProfileError(_ERROR)
	# Serialize even a first insert through the existing Settings rows. DefaultValue
	# uses hash autonaming: db_insert(ignore_if_duplicate=True) can regenerate a
	# colliding name, so it must NOT be used as an upsert.
	frappe.db.get_singles_dict("Jarvis Settings", for_update=True)
	row = frappe.db.get_value(
		"DefaultValue", ROW, ["parent", "defkey", "defvalue"], as_dict=True, for_update=True
	)
	if row is not None and (row.parent != PARENT or row.defkey != KEY):
		raise RuntimeProfileError(_ERROR)
	if not unavailable and row and row.defvalue:
		try:
			previous = validate(json.loads(row.defvalue).get("profile"))
		except (RuntimeProfileError, ValueError, TypeError, AttributeError):
			# A valid authoritative sync may repair a corrupt local receipt.
			previous = None
		if previous is not None and profile != previous:
			# v1 is immutable once accepted. In particular, a refresh cannot broaden
			# the media root or reinterpret stored sequence metadata, even if its
			# digest and newer assignment binding are internally consistent.
			raise RuntimeProfileError(_ERROR)
	if row is None:
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"name": ROW,
				"parent": PARENT,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": KEY,
				"defvalue": "",
			}
		).db_insert()
	if unavailable and row and row.defvalue:
		# Retain last-known-good for diagnosis/rollback, but never use it after
		# Admin explicitly reports an unsupported serving runtime.
		try:
			record["profile"] = json.loads(row.defvalue).get("profile")
			blob = _json(record)
		except (ValueError, TypeError, AttributeError):
			pass
	frappe.db.set_value("DefaultValue", ROW, "defvalue", blob, update_modified=False)
	# Do not publish uncommitted data. Cached snapshots are additionally matched
	# against the durable receipt digest, so a delayed cache write is harmless.
	frappe.db.after_commit.add(_invalidate)
	frappe.db.after_rollback.add(_forget_snapshot)
	_forget_snapshot()
	setattr(frappe.local, _MEMO, (anchor, None if unavailable else profile))


def clear():
	frappe.db.get_singles_dict("Jarvis Settings", for_update=True)
	frappe.db.delete("DefaultValue", {"name": ROW, "parent": PARENT, "defkey": KEY})
	_invalidate()
	frappe.db.after_commit.add(_invalidate)
	frappe.db.after_rollback.add(_forget_snapshot)
	_forget_snapshot()


def get_profile():
	"""One immutable snapshot per request/job, with no Admin RPC.

	Read the durable receipt once before using Redis. A Redis-only pointer can
	otherwise resurrect a stale profile when a writer commits between a cold
	reader's DB read and cache publication. Holding DB locks across a chat turn
	is worse: it blocks connection refreshes. Content-addressed caching avoids
	both races with one small DB read per operation, never per transcript frame.
	"""
	memo = getattr(frappe.local, _MEMO, None)
	if memo is not None:
		if memo[1] is None:
			raise RuntimeProfileError(_ERROR)
		return memo[1]
	anchor = _anchor(frappe.get_single("Jarvis Settings"))
	blob = frappe.db.get_value("DefaultValue", {"name": ROW, "parent": PARENT, "defkey": KEY}, "defvalue")
	if blob is None:
		raise RuntimeProfileError(_ERROR)
	if not isinstance(blob, str) or len(blob.encode()) > MAX_BYTES:
		raise RuntimeProfileError(_ERROR)
	digest = hashlib.sha256(blob.encode()).hexdigest()
	try:
		cached = frappe.cache().get_value(CACHE_KEY, use_local_cache=False)
	except Exception:
		cached = None
	if (
		isinstance(cached, dict)
		and cached.get("digest") == digest
		and cached.get("anchor") == anchor
		and isinstance(cached.get("profile"), RuntimeProfile)
	):
		profile = cached["profile"]
	else:
		try:
			record = json.loads(blob)
		except (ValueError, TypeError):
			raise RuntimeProfileError(_ERROR) from None
		profile = _checked_record(record, anchor)
		try:
			frappe.cache().set_value(
				CACHE_KEY,
				{
					"digest": digest,
					"anchor": anchor,
					"profile": profile,
				},
				expires_in_sec=3600,
			)
		except Exception:
			pass
	setattr(frappe.local, _MEMO, (anchor, profile))
	return profile


def _checked_record(record, anchor, *, allow_unavailable=False):
	if (
		not isinstance(record, dict)
		or record.get("anchor") != anchor
		or (record.get("unavailable") and not allow_unavailable)
	):
		raise RuntimeProfileError(_ERROR)
	return validate(record.get("profile"), anchor)


def get_saved_content_profile():
	"""Only for sanitizing authorized stored HTML, never for gateway requests.

	An unsupported serving runtime may retain a validated, assignment-bound
	profile for old content. A missing, corrupt, or mismatched receipt still fails.
	"""
	try:
		return get_profile()
	except RuntimeProfileError:
		pass
	blob = frappe.db.get_value("DefaultValue", {"name": ROW, "parent": PARENT, "defkey": KEY}, "defvalue")
	if not isinstance(blob, str) or len(blob.encode()) > MAX_BYTES:
		raise RuntimeProfileError(_ERROR)
	try:
		record = json.loads(blob)
	except ValueError:
		raise RuntimeProfileError(_ERROR) from None
	return _checked_record(record, _anchor(frappe.get_single("Jarvis Settings")), allow_unavailable=True)


def ingest_current(data):
	"""Readiness polls may refresh only the already accepted connection."""
	raw = getattr(data, "runtime_profile", None)
	unavailable = data.get("runtime_profile_status") == "unsupported_runtime"
	if not isinstance(data, ConnectionData) and not unavailable:
		return
	frappe.db.get_singles_dict("Jarvis Settings", for_update=True)
	settings = frappe.get_single("Jarvis Settings")
	anchor = _anchor(settings)
	if all(
		data.get(k) == anchor[k]
		for k in (
			"agent_url",
			"tenant_authority_generation",
			"tenant_authority_handle",
		)
	):
		persist(raw, settings, unavailable=unavailable)
		if unavailable:
			raise RuntimeProfileError(_ERROR)


def status():
	"""Server-side bench-console diagnostic; intentionally not whitelisted."""
	blob = frappe.db.get_value("DefaultValue", ROW, "defvalue")
	if not blob:
		return {"state": "missing"}
	try:
		record = json.loads(blob)
		_checked_record(record, _anchor(frappe.get_single("Jarvis Settings")))
		return {
			"state": "configured",
			"profile_id": record["profile"]["profile_id"],
			"revision": record["profile"]["revision"],
			"fetched_at": record["fetched_at"],
		}
	except (RuntimeProfileError, ValueError, KeyError):
		return {"state": "invalid_or_mismatched"}
