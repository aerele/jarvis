"""Bound parser CPU, memory and wall time without limiting the Frappe worker."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import MAX_BYTES
from jarvis.tools._file_scan_worker import RESOURCE_ERROR, WALL_SECONDS


def reader_error(filename, message):
	if isinstance(filename, str) and filename.lower().endswith(".docx"):
		from jarvis.tools._docx_text import DocxReadError

		return DocxReadError(message)
	return InvalidArgumentError(message)


def scan_file(content, filename, sheet=None, cursor=None):
	if len(content) > MAX_BYTES:
		raise reader_error(filename, "File exceeds the 10 MiB input limit. Split it into smaller files.")
	env = os.environ.copy()
	# Use this checkout's parser even when a bench has another editable checkout.
	env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
	try:
		# Security review: fixed interpreter/module argv, shell disabled. All
		# caller-controlled bytes and labels travel only as JSON on stdin.
		# This child is required to enforce parser CPU/memory/wall limits.
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
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			timeout=WALL_SECONDS,
			check=False,
			env=env,
		)
	except subprocess.TimeoutExpired:
		# subprocess.run kills and waits for the child on timeout.
		raise reader_error(filename, RESOURCE_ERROR) from None
	if completed.returncode:
		raise reader_error(filename, RESOURCE_ERROR)
	try:
		response = json.loads(completed.stdout)
	except (ValueError, TypeError):
		if isinstance(filename, str) and filename.lower().endswith(".docx"):
			from jarvis.tools._docx_text import DOCX_ERROR

			raise reader_error(filename, DOCX_ERROR) from None
		raise
	if "error" in response:
		raise reader_error(filename, response["error"])
	return response["result"]
