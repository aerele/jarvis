"""Reorder preparation uses its dedicated metadata-only dispatcher."""

from jarvis.exceptions import PermissionDeniedError


def prepare_reorder_review():
	raise PermissionDeniedError("Reorder preparation requires a current native delegate run.")
