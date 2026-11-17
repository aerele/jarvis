"""Seed the ``Jarvis Activity Rollup`` Single's watermark row (empty value).

``activity_rollup`` reads the watermark ``FOR UPDATE``; a lock on a row that does not
exist is only a gap lock, which would not serialise two runs. An empty ``rolled_through``
means "nothing rolled yet, start at the horizon". Idempotent: an existing row (a rollup
already ran, or this patch did) is left alone. (The rollup also seeds it on first use,
which is what a fresh install relies on: patches do not run there.)"""

from jarvis.activity_rollup import seed_watermark


def execute():
	seed_watermark()
