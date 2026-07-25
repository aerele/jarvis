"""``jarvis__list_app_modules`` — the Custom App Learning scribe's source
MANIFEST tool (self-gated, read-only).

Two shapes, one contract:
  * ``list_app_modules()`` (no ``app``) → the roster of the custom apps an ADMIN
    authorized for THIS run (name/title/version + an approx file/KB size), so the
    delegate can plan its reading without the bench naming an app in the kickoff.
    CX5-2: never the bench's whole custom-app list.
  * ``list_app_modules(app)`` → that app's priority-ordered source file manifest
    (path + size) + its top-level modules + any coverage caveats, so the delegate
    reads SELECTIVELY via ``read_app_source`` instead of pulling the whole tree.

Self-gate (ALL required): the call must resolve to a running app-learning
**scribe** ``Jarvis Agent Run`` bound to the caller's session_key (never a
model-supplied id), the impersonated run-as identity must be admin-tier, and the
named app must pass the custom-apps allowlist AND be in this run's
admin-authorized selection. Contents are NEVER returned here —
this is metadata only; ``read_app_source`` returns file text inside untrusted-
data fences.
"""

from __future__ import annotations

import os

from jarvis.learning import app_source, source_guard
from jarvis.tools import _app_learning_ctx as ctx


def list_app_modules(app: str | None = None) -> dict:
	"""Roster of learnable custom apps (no ``app``) or one app's source manifest."""
	run = ctx.resolve_scribe_run()  # self-gate: scribe run + admin-tier or raise
	# CX5-2: the roster is the ADMIN's selection for THIS run, not the bench's app
	# list — the delegate must not even learn that other custom apps exist.
	allowed = ctx.allowed_source_apps(run["name"])

	if not app or not str(app).strip():
		apps: list[dict] = []
		for name in app_source._installed_custom_apps():
			if name not in allowed:
				continue
			try:
				# Canonicalize + validate the root; a symlinked/relocated app root is
				# never walked (existence is part of the same check).
				src = app_source._resolve_app_root(name)
			except ValueError:
				continue
			approx_files, approx_kb = app_source._approx_size(src)
			apps.append(
				{
					"app": name,
					"title": app_source._app_title(name),
					"version": app_source._app_version(name),
					"approx_files": approx_files,
					"approx_kb": approx_kb,
				}
			)
		return {
			"apps": apps,
			"note": (
				"These are the ONLY apps an admin authorized for this run. Pass one of "
				"these app names to list_app_modules(app) for its source file manifest, "
				"then read_app_source(app, path) selectively. Any other app — core "
				"(frappe/erpnext/hrms/india_compliance/jarvis) or an unselected custom "
				"app — is refused."
			),
		}

	name = str(app).strip()
	ctx.assert_custom_app(name, run["name"])  # allowlist + this run's authorized selection
	src = app_source._resolve_app_root(name)  # canonical + root-validated source dir
	rels, notes = app_source._collect_files(src)
	# CX5-4: drop files whose NAME advertises credentials before they are ever
	# listed, so the delegate cannot even ask for one; the omission is disclosed as a
	# coverage caveat rather than silently narrowing what the wiki claims to cover.
	kept = [rel for rel in rels if not source_guard.is_sensitive_path(rel)]
	skipped_sensitive = len(rels) - len(kept)
	files: list[dict] = []
	for rel in sorted(kept, key=lambda r: (app_source._priority(r), r)):
		try:
			size = os.path.getsize(os.path.join(src, rel))
		except OSError:
			size = 0
		files.append({"path": rel, "size": size})
	modules = sorted({rel.split("/", 1)[0] for rel in kept if "/" in rel})
	caveats = app_source.coverage_caveats(notes)
	if skipped_sensitive:
		caveats.append(
			f"{skipped_sensitive} file(s) skipped: the filename indicates credentials "
			"(never served)"
		)
	return {
		"app": name,
		"title": app_source._app_title(name),
		"version": app_source._app_version(name),
		"file_count": len(files),
		"modules": modules,
		"files": files,
		"coverage_caveats": caveats,
	}
