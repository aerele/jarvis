"""Run full-source analysis in a bounded child, with all source data on stdin."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import MAX_BYTES

WALL_SECONDS = 22  # Leave transport headroom under the plugin's 30-second request timeout.
RESOURCE_ERROR = "File analysis exceeded the resource budget. Split the file, select fewer worksheets or simplify the query. No complete result was returned."


def run_query(content, filename, *, sql=None, sheets=None, max_rows=100):
	if len(content) > MAX_BYTES:
		raise InvalidArgumentError("File exceeds the 10 MiB input limit. Split the file.")
	env = os.environ.copy()
	env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
	try:
		# Security review: fixed interpreter/module argv and no shell. All caller
		# values travel as JSON on stdin. The child enforces CPU/memory/wall limits.
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
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			timeout=WALL_SECONDS,
			check=False,
			env=env,
		)
	except subprocess.TimeoutExpired:
		raise InvalidArgumentError(RESOURCE_ERROR) from None
	if completed.returncode:
		raise InvalidArgumentError(RESOURCE_ERROR)
	try:
		response = json.loads(completed.stdout)
		if "error" in response:
			raise InvalidArgumentError(response["error"])
		return response["result"]
	except (ValueError, KeyError, TypeError):
		raise InvalidArgumentError(RESOURCE_ERROR) from None
