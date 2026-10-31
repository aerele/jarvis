"""One archive operation, under shared CPU/memory/wall and IPC limits."""

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
	signal.alarm(22)
	from jarvis.exceptions import InvalidArgumentError
	from jarvis.tools import _archive_sources as archive

	try:
		line = sys.stdin.buffer.readline(14 * 1024 * 1024 + 1)
		if len(line) > 14 * 1024 * 1024 or not line.endswith(b"\n"):
			raise archive.ArchiveCapacityError("Archive request exceeds its input budget.")
		request = json.loads(line)
		source = archive.ArchiveSource(
			base64.b64decode(request["content"], validate=True),
			request["filename"],
			request.get("payload_format"),
		)
		operation = request["operation"]
		if operation == "manifest":
			result = source.manifest()
		elif operation == "query":
			result = archive.query_archive(
				source,
				request["members"],
				sql=request.get("sql"),
				sheets=request.get("sheets"),
				max_rows=request.get("max_rows", 100),
			)
		elif operation == "read":
			result = archive.scan_archive(
				source, request["member"], sheet=request.get("sheet"), cursor=request.get("cursor")
			)
		else:
			raise archive.ArchiveModeError("Unsupported archive operation.")
		if len(json.dumps(result, ensure_ascii=False)) > 16000:
			raise archive.ArchiveCapacityError(
				"Archive metadata exceeds the output budget. Select a smaller scope for a separate analysis."
			)
		response = {"result": result}
	except MemoryError:
		response = {
			"failure": {
				"code": "ArchiveCapacityError",
				"message": "Archive analysis exceeded the memory budget. No complete answer was returned.",
			}
		}
	except InvalidArgumentError as exc:
		response = {"failure": {"code": type(exc).__name__, "message": str(exc)[:1000]}}
	except Exception:
		response = {
			"failure": {
				"code": "ArchiveFormatError",
				"message": "The archive or its selected data could not be parsed. No complete answer was returned.",
			}
		}
	encoded = archive.encode(response) + b"\n"
	if len(encoded) > 68 * 1024:
		encoded = (
			archive.encode(
				{
					"failure": {
						"code": "ArchiveCapacityError",
						"message": "Archive output exceeds the response budget.",
					}
				}
			)
			+ b"\n"
		)
	sys.stdout.buffer.write(encoded)
	sys.stdout.buffer.flush()


if __name__ == "__main__":
	main()
