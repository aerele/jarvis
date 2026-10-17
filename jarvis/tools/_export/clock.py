"""Export lifecycle timestamps are naive UTC in storage and explicit UTC on the wire."""

from datetime import datetime, timezone


def now_datetime():
	return datetime.now(timezone.utc).replace(tzinfo=None)
