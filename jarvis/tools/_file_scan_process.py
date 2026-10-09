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


def scan_file(content, filename, sheet=None, cursor=None):
	if len(content) > MAX_BYTES:
		raise InvalidArgumentError("File exceeds the 10 MiB input limit. Split it into smaller files.")
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
		raise InvalidArgumentError(RESOURCE_ERROR) from None
	if completed.returncode:
		raise InvalidArgumentError(RESOURCE_ERROR)
	response = json.loads(completed.stdout)
	if "error" in response:
		raise InvalidArgumentError(response["error"])
	return response["result"]
