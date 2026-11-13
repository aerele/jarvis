"""Document Intake: draft stage (placeholder until the draft builder lands)."""


def finish(activity, reading: dict) -> None:
	"""Turn the model's reading into a draft record and finish the Activity row.

	Contract with the worker: ``reading`` may hold a reserved ``"_note"`` key (for example
	"Read 10 of 14 pages"). It lives in the saved reading so it survives draft-only retries.
	``finish`` must pop it before using the reading and append it to the final summary, so
	the uploader (not only managers) sees that part of the file was not read.
	"""
	raise NotImplementedError("Implemented in Task 7")
