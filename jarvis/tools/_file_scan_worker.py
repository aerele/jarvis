"""Disposable parser process. No site connection, retained files or model calls."""

import base64
import json
import resource
import sys

WALL_SECONDS = 12
CPU_SECONDS = 8
MEMORY_BYTES = 512 * 1024 * 1024
RESOURCE_ERROR = "File parsing exceeded the resource budget. Do not retry unchanged; split or simplify the file and report the unread range."


def main():
	resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
	# Linux production workers enforce address space; macOS has no reliable AS limit.
	if sys.platform.startswith("linux"):
		resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
	from jarvis.exceptions import InvalidArgumentError
	from jarvis.tools._file_scan import scan_file

	try:
		request = json.load(sys.stdin)
		result = scan_file(
			base64.b64decode(request["content"], validate=True),
			request["filename"],
			request["sheet"],
			request["cursor"],
		)
		response = {"result": result}
	except MemoryError:
		response = {"error": RESOURCE_ERROR}
	except InvalidArgumentError as exc:
		response = {"error": str(exc)}
	except Exception:
		response = {
			"error": "File format could not be parsed. Re-save it as an unlocked PDF, XLSX or UTF-8 text/CSV."
		}
	json.dump(response, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
	main()
