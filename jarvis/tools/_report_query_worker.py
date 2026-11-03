"""One short-lived report worker. Three bounded frames; no ERP connection."""

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
	from jarvis.tools import _report_query_data as data

	def read(limit):
		line = sys.stdin.buffer.readline(limit + 1)
		if not line or len(line) > limit or not line.endswith(b"\n"):
			raise InvalidArgumentError("Report worker input exceeded its frame budget.")
		return json.loads(line)

	def write(frame, limit):
		line = data.encode(frame) + b"\n"
		if len(line) > limit:
			raise InvalidArgumentError("Report worker output exceeded its frame budget. Narrow the report.")
		sys.stdout.buffer.write(line)
		sys.stdout.buffer.flush()

	db = None
	try:
		request = read(14 * 1024 * 1024)
		schema, rows, total_index = data.decode_report(
			base64.b64decode(request["content"], validate=True),
			legacy_columns=request.get("legacy_columns"),
			add_total_row=request.get("add_total_row", False),
		)
		del request
		write({"schema": schema}, data.MAX_SCHEMA)
		context = read(data.MAX_CONTEXT)
		db, manifest, errors, blocked, proof = data.build_tables(schema, rows, total_index, context)
		del rows
		write({"proofs": proof}, data.MAX_PROOFS + 1024)
		del proof
		query = read(data.MAX_CONTEXT)
		result = data.query_report_tables(
			db,
			manifest,
			schema,
			context,
			sql=query.get("sql"),
			max_rows=query.get("max_rows", 100),
			errors=errors,
			blocked=blocked,
		)
		write({"result": result}, data.MAX_ANSWER)
	except InvalidArgumentError as exc:
		write({"error": str(exc)}, data.MAX_ANSWER)
	except MemoryError:
		write(
			{"error": "Prepared report analysis exceeded the memory budget. Narrow the report."},
			data.MAX_ANSWER,
		)
	except Exception:
		write(
			{"error": "Stored prepared report could not be analyzed. Regenerate it or narrow its scope."},
			data.MAX_ANSWER,
		)
	finally:
		if db is not None:
			db.close()


if __name__ == "__main__":
	main()
