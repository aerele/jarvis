"""Admin-owned catalogs (models, presets): the admin is the only source.

Two ways in, deliberately split:

* ``read()`` is for every hot path (chat send, prepare, pump, picker). It never
  calls the admin and never writes the database: Redis, else this site's
  last-known-good snapshot (refilling Redis), else ``[]``.
* ``refresh()`` fetches from the admin, then updates Redis and the snapshot. It
  runs from the hourly scheduler, after install/migrate, and from the admin-gated
  settings and onboarding endpoints where a fresh answer matters.

``[]`` only happens on a site that has never reached the admin. Callers must
treat it as "catalog unavailable", never as "no models exist".
Both methods never raise: they sit under chat sends and page loads. Redis and
the snapshot are guarded separately, so one failing never hides the other.
"""

import json

import frappe
from frappe.utils import now_datetime

SNAPSHOT_DT = "Jarvis Catalog Snapshot"
CACHE_TTL_S = 2 * 60 * 60  # the hourly refresh renews it well inside this
FETCH_TIMEOUT_S = 5  # an admin that hangs must not hang a settings page
# After a failed fetch, skip the admin for this long: many page loads against a
# slow admin must not each hold a web worker for FETCH_TIMEOUT_S.
FAIL_BACKOFF_S = 30
CATALOG_UNAVAILABLE_MESSAGE = "Models are unavailable right now. Try again in a minute."


class CatalogStore:
	def __init__(self, kind: str, admin_method: str, unwrap_keys: tuple, cache_key: str):
		self.kind = kind
		self.admin_method = admin_method
		self.unwrap_keys = unwrap_keys
		self.cache_key = cache_key

	def read(self) -> list:
		cached = self._cached()
		if cached:
			return cached
		# The snapshot is tried even when Redis failed: an unhealthy cache must not
		# hide a good last-known-good copy.
		try:
			saved = self._load_snapshot()
		except Exception as e:
			# frappe.logger, not log_error: this is reached when the DB is unhealthy.
			frappe.logger().warning("catalog_store.read(%s) snapshot unavailable: %s", self.kind, e)
			return []
		if saved:
			self._cache(saved)
		return saved

	def refresh(self, force: bool = False) -> list:
		"""``force`` ignores the failure backoff (an explicit Retry click)."""
		if not force and self._backing_off():
			return self.read()
		try:
			catalog = self._fetch_from_admin()
		except Exception:
			self._back_off()
			frappe.log_error(title=f"catalog_store: {self.kind} catalog fetch from admin failed")
			return self.read()
		if not catalog:
			# Admin answered but served nothing. Keep the last good copy rather than
			# blanking every picker; the log tells "down" and "empty" apart.
			frappe.log_error(title=f"catalog_store: admin returned an empty {self.kind} catalog")
			return self.read()
		self._cache(catalog)
		try:
			self._save_snapshot(catalog)
		except Exception:
			frappe.log_error(title=f"catalog_store: saving the {self.kind} catalog snapshot failed")
		return catalog

	def _backing_off(self) -> bool:
		try:
			return bool(frappe.cache().get_value(self._fail_key, expires=True))
		except Exception:
			return False

	def _back_off(self) -> None:
		try:
			frappe.cache().set_value(self._fail_key, 1, expires_in_sec=FAIL_BACKOFF_S)
		except Exception:
			pass

	@property
	def _fail_key(self) -> str:
		return f"{self.cache_key}:failed"

	def _cached(self) -> list:
		try:
			return frappe.cache().get_value(self.cache_key, expires=True) or []
		except Exception as e:
			frappe.logger().warning("catalog_store: redis read failed for %s: %s", self.kind, e)
			return []

	def _cache(self, catalog: list) -> None:
		try:
			frappe.cache().set_value(self.cache_key, catalog, expires_in_sec=CACHE_TTL_S)
		except Exception as e:
			frappe.logger().warning("catalog_store: redis write failed for %s: %s", self.kind, e)

	def _fetch_from_admin(self) -> list:
		from jarvis.admin_client import _m, _post_guest

		answer = _post_guest(path=_m(self.admin_method), body={}, timeout_s=FETCH_TIMEOUT_S)
		if isinstance(answer, dict):
			answer = next((answer[k] for k in self.unwrap_keys if answer.get(k)), [])
		return answer if isinstance(answer, list) else []

	def _load_snapshot(self) -> list:
		raw = frappe.db.get_single_value(SNAPSHOT_DT, self._field)
		if not raw:
			return []
		value = json.loads(raw) if isinstance(raw, str) else raw
		return value if isinstance(value, list) else []

	def _save_snapshot(self, catalog: list) -> None:
		# The catalog is rewritten only when it changed; the fetch time always moves
		# so "how stale is this site's copy" is answerable during an outage.
		new = json.dumps(catalog, sort_keys=True)
		old = frappe.db.get_single_value(SNAPSHOT_DT, self._field)
		if old and not isinstance(old, str):
			old = json.dumps(old, sort_keys=True)
		values = {f"{self._field}_fetched_at": now_datetime()}
		if old != new:
			values[self._field] = new
		frappe.db.set_single_value(SNAPSHOT_DT, values)

	@property
	def _field(self) -> str:
		return f"{self.kind}_catalog"


MODELS = CatalogStore(
	kind="model",
	admin_method="fleet.provider_catalog.get_provider_catalog",
	unwrap_keys=("data",),
	cache_key="jarvis:model_catalog",
)
PRESETS = CatalogStore(
	kind="preset",
	admin_method="billing.catalog.get_preset_catalog",
	unwrap_keys=("data", "catalog", "presets"),
	cache_key="jarvis:preset_catalog",
)


def refresh_all() -> None:
	"""Hourly scheduler job: both catalogs from the admin."""
	MODELS.refresh()
	PRESETS.refresh()


def enqueue_refresh_all() -> None:
	"""after_install / after_migrate: queue the refresh so a slow admin never
	blocks an install or a deploy (two fetches can take 10 s at worst)."""
	frappe.enqueue("jarvis.catalog_store.refresh_all", queue="short", enqueue_after_commit=True)
