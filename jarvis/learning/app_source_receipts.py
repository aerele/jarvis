"""Server-owned receipts for Custom App Learning source evidence.

The delegate can see redacted source text, but it must never be able to invent
the evidence that permits an Org-wiki write.  This module keeps that authority
on the bench.  A receipt is HMAC-signed with the site encryption key and binds
one run to its immutable source selection, run-as permission profile, approved
source root/manifest, and every successful source read's exact bytes and digest.

Only hashes, byte counts, and safe path metadata are stored.  Raw source text
never enters the receipt, a chat receipt, realtime event, or server log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source, source_guard

RUN = "Jarvis Agent Run"
SELECTION_SCHEMA = "jarvis-app-source-selection-v1"
RECEIPT_SCHEMA = "jarvis-app-source-receipt-v1"
MAX_RECEIPT_CHARS = 2 * 1024 * 1024
MAX_CITED_PATHS = 64
# A receipt expires even if a worker is left running.  The run-status check is
# still authoritative; the TTL limits a signed receipt's usefulness during an
# abnormal worker/session lifetime.
SOURCE_RECEIPT_TTL_SECONDS = 90 * 60


def _canonical(value: Any) -> str:
	return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: str | bytes) -> str:
	data = value if isinstance(value, bytes) else value.encode("utf-8")
	return hashlib.sha256(data).hexdigest()


def _sign(payload: dict) -> str:
	"""Sign server receipt state. Missing server key is a hard refusal.

	The key is never returned or logged.  A model can neither read nor submit this
	signature because the tool accepts no receipt argument.
	"""
	key = str(frappe.conf.get("encryption_key") or "").encode("utf-8")
	if not key:
		raise InvalidArgumentError("app-source receipt signing is unavailable; refusing the write")
	return hmac.new(key, _canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def _signed(payload: dict) -> dict:
	out = dict(payload)
	out["signature"] = _sign(payload)
	return out


def _unsigned_and_verified(raw: str | None, *, expected_schema: str) -> dict:
	if not isinstance(raw, str) or not raw or len(raw) > MAX_RECEIPT_CHARS:
		raise InvalidArgumentError("app-source receipt is missing or invalid; refusing the write")
	try:
		value = json.loads(raw)
	except Exception as e:
		raise InvalidArgumentError("app-source receipt is unreadable; refusing the write") from e
	if not isinstance(value, dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the write")
	signature = value.pop("signature", None)
	if value.get("schema") != expected_schema or not isinstance(signature, str):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the write")
	if not hmac.compare_digest(signature, _sign(value)):
		raise InvalidArgumentError("app-source receipt signature is invalid; refusing the write")
	return value


def permission_profile_digest(raw: str | None) -> str:
	"""Return the launch-time permission-profile hash, or fail closed."""
	try:
		value = json.loads(raw or "{}")
	except Exception as e:
		raise InvalidArgumentError("run permission profile is unreadable; refusing source access") from e
	digest = value.get("hash") if isinstance(value, dict) else ""
	if not isinstance(digest, str) or len(digest) != 64:
		raise InvalidArgumentError("run permission profile is missing; refusing source access")
	try:
		int(digest, 16)
	except ValueError as e:
		raise InvalidArgumentError("run permission profile is invalid; refusing source access") from e
	return digest.lower()


def permission_profile_for_user(user: str) -> str:
	"""Build a cache-independent run-as profile for the source boundary.

	Source access must refuse a real role/User Permission change, but a cache
	failure must not manufacture one. Read the authoritative role and User
	Permission rows directly. The scheduler uses this exact representation at
	launch, and the tools recompute it before every source read or wiki write.
	"""
	if not isinstance(user, str) or not user:
		raise InvalidArgumentError("run-as user is missing; refusing source access")
	roles = sorted(
		{
			str(row.get("role") or "")
			for row in frappe.db.get_all("Has Role", filters={"parent": user}, fields=["role"])
			if str(row.get("role") or "")
		}
	)
	permissions: dict[str, list[dict]] = {}
	rows = frappe.db.get_all(
		"User Permission",
		filters={"user": user},
		fields=[
			"name",
			"allow",
			"for_value",
			"is_default",
			"apply_to_all_doctypes",
			"applicable_for",
			"hide_descendants",
		],
		order_by="name asc",
	)
	for row in rows:
		allow = str(row.get("allow") or "")
		value = str(row.get("for_value") or "")
		if not allow or not value:
			continue
		permissions.setdefault(allow, []).append(
			{
				"value": value,
				"default": int(row.get("is_default") or 0),
				"all": int(row.get("apply_to_all_doctypes") or 0),
				"applicable_for": str(row.get("applicable_for") or ""),
				"hide_descendants": int(row.get("hide_descendants") or 0),
			}
		)
	summary = {"roles": roles, "user_permissions": permissions}
	return _canonical({"hash": _sha256(_canonical(summary)), **summary})


def _launch_window(run_name: str, started_at=None) -> tuple[str, str]:
	"""Return the signed source window from launch-owned server state.

	A receipt must not mint a fresh lifetime on its first use. The run is already
	inserted before the scheduler signs its selection, so the durable ``started_at``
	row is the authority. Tests may pass it explicitly to construct an old launch.
	"""
	if started_at is None:
		started_at = frappe.db.get_value(RUN, run_name, "started_at")
	try:
		issued_at = frappe.utils.get_datetime(started_at)
	except Exception as e:
		raise InvalidArgumentError("run launch time is missing; refusing source access") from e
	if not issued_at:
		raise InvalidArgumentError("run launch time is missing; refusing source access")
	expires_at = frappe.utils.add_to_date(issued_at, seconds=SOURCE_RECEIPT_TTL_SECONDS)
	return issued_at.isoformat(), expires_at.isoformat()


def selection_payload(run_name: str, apps: list[str], profile_digest: str, *, started_at=None) -> dict:
	"""Build the immutable server-side selection stamp for a newly launched run."""
	if not run_name or not apps or not profile_digest:
		raise InvalidArgumentError("app-source selection is incomplete; refusing source access")
	issued_at, expires_at = _launch_window(run_name, started_at)
	return {
		"schema": SELECTION_SCHEMA,
		"run": run_name,
		"apps": list(apps),
		"permission_profile_digest": profile_digest,
		"issued_at": issued_at,
		"expires_at": expires_at,
	}


def encode_selection(run_name: str, apps: list[str], profile_digest: str, *, started_at=None) -> str:
	return _canonical(_signed(selection_payload(run_name, apps, profile_digest, started_at=started_at)))


def selection_from_row(row: dict, run_name: str, profile_digest: str) -> dict:
	"""Validate the launch-owned selection stored on a locked run row."""
	payload = _unsigned_and_verified(row.get("source_selection_json"), expected_schema=SELECTION_SCHEMA)
	if payload.get("run") != run_name or payload.get("permission_profile_digest") != profile_digest:
		raise InvalidArgumentError("app-source selection does not match this run; refusing source access")
	apps = payload.get("apps")
	if not isinstance(apps, list) or not apps or any(not isinstance(app, str) or not app for app in apps):
		raise InvalidArgumentError("app-source selection is invalid; refusing source access")
	if len(set(apps)) != len(apps):
		raise InvalidArgumentError("app-source selection is invalid; refusing source access")
	try:
		issued_at = frappe.utils.get_datetime(payload.get("issued_at"))
		expires_at = frappe.utils.get_datetime(payload.get("expires_at"))
	except Exception as e:
		raise InvalidArgumentError("app-source selection expiry is invalid; refusing source access") from e
	if not issued_at or not expires_at or expires_at <= issued_at:
		raise InvalidArgumentError("app-source selection expiry is invalid; refusing source access")
	if frappe.utils.now_datetime() > expires_at:
		raise InvalidArgumentError("app-source selection expired; refusing source access")
	payload["selection_digest"] = _sha256(_canonical(payload))
	return payload


def _manifest(app: str) -> dict:
	"""Return a metadata-only identity for the presently approved app root."""
	root = app_source._resolve_app_root(app)
	rels, notes = app_source._collect_files(root)
	files: list[dict] = []
	for rel in sorted(rels):
		if source_guard.is_sensitive_path(rel):
			continue
		full = os.path.join(root, rel)
		# _collect_files already checks this. Re-check before stat so a source-tree race
		# cannot make receipt construction follow a new symlink.
		if os.path.islink(full) or not app_source._is_contained(full, root):
			continue
		try:
			stat = os.stat(full, follow_symlinks=False)
		except OSError:
			continue
		files.append({"path": rel, "bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
	coverage = app_source.coverage_caveats(notes)
	skipped_sensitive = len(rels) - len(files)
	if skipped_sensitive:
		coverage.append(f"{skipped_sensitive} credential-named file(s) excluded")
	root_identity = _sha256(_canonical({"app": app, "root": root}))
	identity = _sha256(_canonical({"root_identity": root_identity, "files": files, "coverage": coverage}))
	return {
		"root_identity": root_identity,
		"manifest_identity": identity,
		"files": files,
		"coverage": coverage,
	}


def _base_receipt(run_name: str, selection: dict, profile_digest: str) -> dict:
	return {
		"schema": RECEIPT_SCHEMA,
		"run": run_name,
		"status": "running",
		"issued_at": selection["issued_at"],
		"expires_at": selection["expires_at"],
		"selection_digest": selection["selection_digest"],
		"permission_profile_digest": profile_digest,
		"apps": {},
		"writes": {},
	}


def _receipt_from_row(row: dict, run_name: str, selection: dict, profile_digest: str) -> dict:
	raw = row.get("source_receipt_json")
	if not raw:
		return _base_receipt(run_name, selection, profile_digest)
	payload = _unsigned_and_verified(raw, expected_schema=RECEIPT_SCHEMA)
	if (
		payload.get("run") != run_name
		or payload.get("status") != "running"
		or payload.get("selection_digest") != selection["selection_digest"]
		or payload.get("permission_profile_digest") != profile_digest
	):
		raise InvalidArgumentError("app-source receipt does not match this run; refusing the write")
	try:
		issued_at = frappe.utils.get_datetime(payload.get("issued_at"))
		expires_at = frappe.utils.get_datetime(payload.get("expires_at"))
	except Exception as e:
		raise InvalidArgumentError("app-source receipt expiry is invalid; refusing the write") from e
	if not issued_at or not expires_at or expires_at <= issued_at:
		raise InvalidArgumentError("app-source receipt expiry is invalid; refusing the write")
	if frappe.utils.now_datetime() > expires_at:
		raise InvalidArgumentError("app-source receipt expired; refusing the write")
	if not isinstance(payload.get("apps"), dict) or not isinstance(payload.get("writes"), dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the write")
	return payload


def _empty_app_state(manifest: dict) -> dict:
	return {
		"root_identity": manifest["root_identity"],
		"manifest_identity": manifest["manifest_identity"],
		"files": manifest["files"],
		"coverage": manifest["coverage"],
		"reads": {},
	}


def prepare_manifest(
	row: dict, run_name: str, app: str, selection: dict, profile_digest: str
) -> tuple[dict, dict]:
	"""Load receipt state and bind one app to its current approved manifest.

	When the root or manifest changed since an earlier read, all old reads for that
	app are discarded. A later wiki write then fails until the model reads the
	cited source again. Old write markers stay so a late retry cannot overwrite a
	page from a later run.
	"""
	if app not in selection["apps"]:
		raise InvalidArgumentError("app is outside this run's immutable source selection")
	receipt = _receipt_from_row(row, run_name, selection, profile_digest)
	manifest = _manifest(app)
	state = receipt["apps"].get(app)
	if not isinstance(state, dict) or (
		state.get("root_identity") != manifest["root_identity"]
		or state.get("manifest_identity") != manifest["manifest_identity"]
	):
		receipt["apps"][app] = _empty_app_state(manifest)
	return receipt, manifest


def encode_receipt(receipt: dict) -> str:
	payload = dict(receipt)
	payload.pop("signature", None)
	encoded = _canonical(_signed(payload))
	if len(encoded) > MAX_RECEIPT_CHARS:
		raise InvalidArgumentError("app-source receipt is too large; refusing further source reads")
	return encoded


def record_successful_read(receipt: dict, app: str, path: str, *, bytes_read: int, digest: str) -> None:
	"""Add a server-observed source read. No caller-supplied digest is accepted."""
	state = receipt.get("apps", {}).get(app)
	if not isinstance(state, dict):
		raise InvalidArgumentError("app-source receipt is missing this app; refusing source access")
	known = {entry.get("path"): entry for entry in state.get("files", []) if isinstance(entry, dict)}
	entry = known.get(path)
	if not isinstance(entry, dict) or int(entry.get("bytes") or -1) != int(bytes_read):
		raise InvalidArgumentError("source changed while it was read; no usable receipt was issued")
	reads = state.get("reads")
	if not isinstance(reads, dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing source access")
	reads[path] = {"sha256": digest, "bytes": int(bytes_read)}


def _canonical_paths(paths) -> list[str]:
	if not isinstance(paths, list) or not paths or len(paths) > MAX_CITED_PATHS:
		raise InvalidArgumentError(
			"each wiki page must cite 1 to 64 source paths that were read successfully"
		)
	try:
		out = [app_source.canonical_source_path(path) for path in paths]
	except ValueError as e:
		raise InvalidArgumentError(str(e)) from e
	if len(set(out)) != len(out):
		raise InvalidArgumentError("a wiki page must not cite the same source path twice")
	return out


def _app_state_is_current(state: dict, manifest: dict) -> bool:
	return (
		state.get("root_identity") == manifest["root_identity"]
		and state.get("manifest_identity") == manifest["manifest_identity"]
	)


def verify_page_sources(
	row: dict,
	*,
	run_name: str,
	app: str,
	source_paths,
	selection: dict,
	profile_digest: str,
) -> tuple[dict, list[str], str, list[str]]:
	"""Verify cited reads against the current on-disk source before a wiki write."""
	receipt = _receipt_from_row(row, run_name, selection, profile_digest)
	state = receipt.get("apps", {}).get(app)
	if not isinstance(state, dict):
		raise InvalidArgumentError("no server source receipt exists for this app; read cited source first")
	manifest = _manifest(app)
	if not _app_state_is_current(state, manifest):
		raise InvalidArgumentError("the cited source is stale; read the current source before writing")
	paths = _canonical_paths(source_paths)
	reads = state.get("reads") if isinstance(state.get("reads"), dict) else {}
	evidence: list[dict] = []
	for path in paths:
		read = reads.get(path)
		if not isinstance(read, dict):
			raise InvalidArgumentError("each cited source path must have a complete server receipt")
		# Re-read locally only to hash it. The bytes never enter a model, receipt,
		# chat transcript, realtime event, or log on this path.
		try:
			_unused_text, size, digest = app_source.read_source_file_with_digest(app, path)
		except ValueError as e:
			raise InvalidArgumentError("the cited source is stale or unavailable; read it again") from e
		if int(read.get("bytes") or -1) != size or read.get("sha256") != digest:
			raise InvalidArgumentError("the cited source changed after it was read; refusing the wiki write")
		evidence.append({"path": path, "bytes": size, "sha256": digest})
	evidence_digest = _sha256(
		_canonical(
			{
				"run": run_name,
				"app": app,
				"root_identity": manifest["root_identity"],
				"manifest_identity": manifest["manifest_identity"],
				"files": evidence,
			}
		)
	)
	return receipt, paths, evidence_digest, list(state.get("coverage") or [])


def page_write_key(app: str, stable_key: str) -> str:
	return _sha256(_canonical({"app": app, "key": stable_key}))


def page_body_digest(
	*,
	app: str,
	stable_key: str,
	title: str,
	page_type: str,
	mode: str,
	body: str,
	source_paths: list[str],
	evidence_digest: str,
) -> str:
	return _sha256(
		_canonical(
			{
				"app": app,
				"key": stable_key,
				"title": title,
				"page_type": page_type,
				"mode": mode,
				"body": body,
				"source_paths": source_paths,
				"evidence_digest": evidence_digest,
			}
		)
	)


def retry_state(receipt: dict, *, app: str, stable_key: str, body_digest: str, evidence_digest: str) -> bool:
	"""True for an exact same-run retry; reject a conflicting repeat."""
	writes = receipt.get("writes")
	if not isinstance(writes, dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the wiki write")
	prior = writes.get(page_write_key(app, stable_key))
	if not prior:
		return False
	if not isinstance(prior, dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the wiki write")
	if prior.get("body_digest") == body_digest and prior.get("evidence_digest") == evidence_digest:
		return True
	raise InvalidArgumentError(
		"this stable page key was already written by this run; only an exact retry is allowed"
	)


def record_page_write(
	receipt: dict,
	*,
	app: str,
	stable_key: str,
	slug: str,
	body_digest: str,
	evidence_digest: str,
) -> None:
	writes = receipt.get("writes")
	if not isinstance(writes, dict):
		raise InvalidArgumentError("app-source receipt is invalid; refusing the wiki write")
	writes[page_write_key(app, stable_key)] = {
		"app": app,
		"key": stable_key,
		"slug": slug,
		"body_digest": body_digest,
		"evidence_digest": evidence_digest,
	}


def coverage_attestation(source_count: int, caveats: list[str]) -> str:
	"""A server-generated, bounded page footer. It discloses evidence scope only."""
	caveat = "; ".join(str(item) for item in caveats if item)[:300]
	text = (
		"\n\n---\n"
		f"_Source coverage: {source_count} server-verified cited file(s) from the selected app snapshot. "
		"This page is limited to those citations and does not claim full-app coverage._"
	)
	if caveat:
		text += f"\n_Snapshot caveats: {caveat}._"
	return text
