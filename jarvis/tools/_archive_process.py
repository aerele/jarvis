"""Fixed worker invocation; expanded archive contents never enter the main process."""

import base64

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._archive_sources import ERRORS, MAX_INPUT, ArchiveCapacityError, ArchiveFormatError
from jarvis.tools._report_query_process import Deadline, ReportWorker


def run_archive(content, filename, *, deadline=None, **operation):
	if len(content) > MAX_INPUT:
		raise ArchiveCapacityError("Archive exceeds the 10 MiB input limit.")
	deadline = deadline or Deadline()
	try:
		with ReportWorker(deadline, worker_module="jarvis.tools._archive_worker") as worker:
			worker.send(
				{"content": base64.b64encode(content).decode("ascii"), "filename": filename, **operation},
				14 * 1024 * 1024,
			)
			frame = worker.receive(68 * 1024)
	except InvalidArgumentError:
		raise ArchiveCapacityError(
			"Archive analysis exceeded its worker or output budget. No complete answer was returned. Do not retry unchanged."
		) from None
	if "failure" in frame:
		failure = frame["failure"]
		if (
			not isinstance(failure, dict)
			or failure.get("code") not in ERRORS
			or not isinstance(failure.get("message"), str)
		):
			raise ArchiveFormatError("The archive worker returned an invalid failure response.")
		raise ERRORS[failure["code"]](failure["message"][:1000])
	result = frame.get("result")
	if not isinstance(result, dict):
		raise ArchiveFormatError("The archive worker returned an invalid response.")
	return result
