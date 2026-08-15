from dataclasses import dataclass, field


@dataclass
class ExportModel:
	"""Canonical fill->export seam: resolvers produce it, renderers consume it.

	Renderers must never touch a data source - they read only this. ``total`` is
	the TRUE, permission-filtered source count (never a capped/shown count), so a
	renderer or tool can disclose it honestly. ``meta`` carries render hints
	(e.g. ``title``) and disclosure flags (e.g. ``cells_truncated``)."""

	columns: list[str]
	rows: list[list]
	total: int
	meta: dict = field(default_factory=dict)
