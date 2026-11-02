"""Bound parser CPU, memory and wall time without limiting the Frappe worker."""

import base64
import json
import subprocess
import sys

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import MAX_BYTES
from jarvis.tools._file_scan_worker import RESOURCE_ERROR, WALL_SECONDS
from jarvis.tools._source_process import check_exit, decode_result, fail_worker, worker_env, worker_slot


def reader_error(filename, message):
	if isinstance(filename, str) and filename.lower().endswith(".docx"):
		from jarvis.tools._docx_text import DocxReadError

		return DocxReadError(message)
	return InvalidArgumentError(message)


def scan_file(content, filename, sheet=None, cursor=None):
	try:
		return _scan_file(content, filename, sheet, cursor)
	except InvalidArgumentError as error:
		raise reader_error(filename, str(error)) from None


def _scan_file(content, filename, sheet=None, cursor=None):
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
				# Source-controlled parser warnings can be arbitrarily large. Keep only
				# the bounded JSON stdout protocol; stderr is neither evidence nor a
				# safe diagnostic channel for the parent web worker.
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
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
