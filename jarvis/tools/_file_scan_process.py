"""Bound parser CPU, memory and wall time without limiting the Frappe worker."""

import base64
import json
import subprocess
import sys

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import MAX_BYTES
from jarvis.tools._file_scan_worker import RESOURCE_ERROR, WALL_SECONDS
from jarvis.tools._source_process import check_exit, decode_result, fail_worker, worker_env, worker_slot


def scan_file(content, filename, sheet=None, cursor=None):
	if len(content) > MAX_BYTES:
		raise InvalidArgumentError("File exceeds the 10 MiB input limit. Split it into smaller files.")
	env = worker_env()
	try:
		# Security review: fixed interpreter/module argv, shell disabled. All
		# caller-controlled bytes and labels travel only as JSON on stdin.
		# This child is required to enforce parser CPU/memory/wall limits.
		with worker_slot() as slot:
			completed = subprocess.run(  # nosemgrep: frappe-subprocess-exec
				[sys.executable, "-m", "jarvis.tools._file_scan_worker"],
				input=json.dumps(
					{
						"content": base64.b64encode(content).decode("ascii"),
						"filename": filename,
						"sheet": sheet,
						"cursor": cursor,
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
		# subprocess.run kills and waits for the child on timeout.
		raise InvalidArgumentError(RESOURCE_ERROR) from None
	except (OSError, UnicodeError):
		fail_worker("scan", "process_start_or_encoding")
	check_exit(completed, "scan", RESOURCE_ERROR)
	return decode_result(completed, "scan")
