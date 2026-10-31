"""Disposable spreadsheet worker. No Frappe site, disk cache or external calls."""

import base64
import json
import resource
import signal
import sys


def main():
	resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
	resource.setrlimit(resource.RLIMIT_CPU, (16, 16))
	if sys.platform.startswith("linux"):
		resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
	# Keep a wall bound even if the parent dies before its subprocess timeout.
	signal.alarm(22)
	from jarvis.exceptions import InvalidArgumentError
	from jarvis.tools._spreadsheet_process import RESOURCE_ERROR
	from jarvis.tools._spreadsheet_query import query_spreadsheet

	try:
		request = json.load(sys.stdin)
		result = query_spreadsheet(
			base64.b64decode(request["content"], validate=True),
			request["filename"],
			sql=request["sql"],
			sheets=request["sheets"],
			max_rows=request["max_rows"],
		)
		response = {"result": result}
	except MemoryError:
		response = {"error": RESOURCE_ERROR}
	except InvalidArgumentError as exc:
		response = {"error": str(exc)}
	except Exception:
		response = {
			"error": "File analysis failed. Supply unlocked XLSX or valid UTF-8 CSV/TSV, JSON, MD or TXT."
		}
	json.dump(response, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
	main()
