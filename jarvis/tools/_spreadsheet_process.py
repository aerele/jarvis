"""Run full-source analysis in a bounded child, with all source data on stdin."""

import base64
import json
import subprocess
import sys

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import MAX_BYTES
from jarvis.tools._source_process import check_exit, decode_result, fail_worker, worker_env, worker_slot

WALL_SECONDS = 22  # Leave transport headroom under the plugin's 30-second request timeout.
RESOURCE_ERROR = "File analysis exceeded the resource budget. Split the file, select fewer worksheets or simplify the query. No complete result was returned."


def run_query(content, filename, *, sql=None, sheets=None, max_rows=100):
	if len(content) > MAX_BYTES:
		raise InvalidArgumentError("File exceeds the 10 MiB input limit. Split the file.")
	env = worker_env()
	try:
		# Security review: fixed interpreter/module argv and no shell. All caller
		# values travel as JSON on stdin. The child enforces CPU/memory/wall limits.
		with worker_slot() as slot:
			completed = subprocess.run(  # nosemgrep: frappe-subprocess-exec
				[sys.executable, "-m", "jarvis.tools._spreadsheet_worker"],
				input=json.dumps(
					{
						"content": base64.b64encode(content).decode("ascii"),
						"filename": filename,
						"sql": sql,
						"sheets": sheets,
						"max_rows": max_rows,
					}
				),
				shell=False,
				text=True,
				encoding="utf-8",
				pass_fds=(slot,),
				capture_output=True,
				timeout=WALL_SECONDS,
				check=False,
				env=env,
			)
	except subprocess.TimeoutExpired:
		raise InvalidArgumentError(RESOURCE_ERROR) from None
	except (OSError, UnicodeError):
		fail_worker("query", "process_start_or_encoding")
	check_exit(completed, "query", RESOURCE_ERROR)
	return decode_result(completed, "query")
